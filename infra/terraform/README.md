# AWS version (Terraform)

**Not deployed.** These files describe how PriorAuth Gateway would run on AWS. They pass
`terraform validate` in CI. Running `terraform plan` needs your own AWS credentials; `apply` would
create billable resources (the NAT gateway, RDS and Amazon MQ cost money even when idle).

| Local (docker-compose) | AWS (this folder)                                         |
| ---------------------- | --------------------------------------------------------- |
| api container          | ECS Fargate service behind an Application Load Balancer   |
| worker container       | ECS Fargate service, no inbound traffic                   |
| mock-payer container   | ECS Fargate service, found by the worker via Cloud Map    |
| PostgreSQL             | RDS for PostgreSQL 16, private subnets, encrypted         |
| RabbitMQ               | Amazon MQ for RabbitMQ, private, AMQPS                    |
| `.env` file            | One Secrets Manager secret, injected into the tasks by ECS |
| stdout JSON logs       | CloudWatch Logs, one group per service                    |

Layout: a VPC with two public subnets (load balancer, NAT) and two private subnets (everything
else). Security groups only let each tier talk to the one in front of it: internet → ALB → API;
API and worker → Postgres and the broker.

## Try it

```bash
cd infra/terraform
terraform init -backend=false
terraform validate
terraform plan        # needs AWS credentials; creates nothing
```

To actually deploy: configure the S3 backend in `versions.tf`, run `terraform apply`, push the image
to the `ecr_repository_url` output with the tag in `image_tag`, then open the `api_url` output.

## Known gaps (on purpose, to keep it small)

- HTTP only. Real use needs an ACM certificate and an HTTPS listener.
- Single NAT gateway and single-instance broker: cheap, not highly available.
- Generated passwords live in Terraform state, so the state must be remote and encrypted.
- Traces are not exported in AWS. The next step is an ADOT collector sidecar sending to X-Ray.
- The queue is Amazon MQ, so the app code runs unchanged. Moving to SQS would need an SQS adapter
  for `priorauth.queue` (SQS has native DLQs via a redrive policy, and delays via `DelaySeconds`).
