# ECS Fargate: the API behind a load balancer, the worker, and (optionally) the mock health plan.

resource "aws_ecr_repository" "app" {
  name                 = var.project
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "mock-payer"])
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = 14
}

# --- IAM: the execution role pulls the image and reads the one secret; the task role has no AWS access ---

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_app_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }
}

resource "aws_iam_role_policy" "read_app_secret" {
  name   = "read-app-secret"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_app_secret.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# --- Service discovery, so the worker reaches the mock payer at mock-payer.<namespace> ---

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.project}.internal"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "mock_payer" {
  name = "mock-payer"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

# --- Task definitions ---

locals {
  image          = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  payer_base_url = var.deploy_mock_payer ? "http://mock-payer.${aws_service_discovery_private_dns_namespace.main.name}:8081" : var.payer_base_url

  common_env = [
    { name = "PAYER_BASE_URL", value = local.payer_base_url },
    { name = "FHIR_BASE_URL", value = var.fhir_base_url },
    { name = "MAX_ATTEMPTS", value = "3" },
    { name = "RETRY_DELAY_MS", value = "5000" },
    { name = "LOG_LEVEL", value = "INFO" },
  ]
  secret_keys = ["DATABASE_URL", "AMQP_URL", "JWT_SECRET", "DEMO_USERS", "API_KEYS"]
  secrets     = [for k in local.secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]

  log_config = { for svc in ["api", "worker", "mock-payer"] : svc => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.app[svc].name
      awslogs-region        = var.region
      awslogs-stream-prefix = svc
    }
  } }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name             = "api"
    image            = local.image
    essential        = true
    portMappings     = [{ containerPort = 8080, protocol = "tcp" }]
    environment      = concat(local.common_env, [{ name = "SERVICE_NAME", value = "priorauth-api" }])
    secrets          = local.secrets
    logConfiguration = local.log_config["api"]
    # The image's default command runs the Alembic migrations, then the API.
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name             = "worker"
    image            = local.image
    essential        = true
    command          = ["python", "-m", "priorauth.worker.main"]
    environment      = concat(local.common_env, [{ name = "SERVICE_NAME", value = "priorauth-worker" }])
    secrets          = local.secrets
    logConfiguration = local.log_config["worker"]
  }])
}

resource "aws_ecs_task_definition" "mock_payer" {
  count                    = var.deploy_mock_payer ? 1 : 0
  family                   = "${local.name}-mock-payer"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name             = "mock-payer"
    image            = local.image
    essential        = true
    command          = ["uvicorn", "priorauth.mock_payer.main:app", "--host", "0.0.0.0", "--port", "8081"]
    portMappings     = [{ containerPort = 8081, protocol = "tcp" }]
    logConfiguration = local.log_config["mock-payer"]
  }])
}

# --- Load balancer for the API ---

resource "aws_lb" "api" {
  name               = substr(local.name, 0, 32)
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api", 0, 32)
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path                = "/health/ready"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- Services ---

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

resource "aws_ecs_service" "mock_payer" {
  count           = var.deploy_mock_payer ? 1 : 0
  name            = "mock-payer"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock_payer[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  service_registries {
    registry_arn = aws_service_discovery_service.mock_payer.arn
  }
}
