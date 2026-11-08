"""Fetch a Patient from a public FHIR test server and submit a prior auth request that references it.

    python scripts/fetch_fhir_patient.py                     # finds a patient on hapi.fhir.org
    python scripts/fetch_fhir_patient.py --patient-id 123    # use a specific one
    python scripts/fetch_fhir_patient.py --no-submit         # only fetch and print

The HAPI public server holds test data uploaded by anyone. It is not real patient data, but it is
public and changes all the time, so a given id may disappear.

The gateway's worker resolves `patientReference` against FHIR_BASE_URL, so this works end to end only
when the stack points at the same server (the default in .env.example).
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

SAMPLE = Path(__file__).with_name("sample_request.json")


def find_patient(fhir: httpx.Client, patient_id: str | None) -> dict:
    if patient_id:
        r = fhir.get(f"/Patient/{patient_id}")
        r.raise_for_status()
        return r.json()
    # Search for recently updated patients that have a name and a birth date (what the gateway needs).
    r = fhir.get("/Patient", params={"_count": 20, "_sort": "-_lastUpdated", "birthdate:missing": "false"})
    r.raise_for_status()
    for entry in r.json().get("entry", []):
        p = entry["resource"]
        if p.get("name") and p.get("id"):
            return p
    sys.exit("no usable Patient found; try --patient-id")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fhir-base", default="https://hapi.fhir.org/baseR4")
    ap.add_argument("--patient-id")
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--user", default="clinician")
    ap.add_argument("--password", default="clinician-dev-pw")
    ap.add_argument("--no-submit", action="store_true")
    args = ap.parse_args()

    with httpx.Client(base_url=args.fhir_base, timeout=15, headers={"Accept": "application/fhir+json"}) as fhir:
        patient = find_patient(fhir, args.patient_id)
    name = patient["name"][0]
    print(f"Patient/{patient['id']}: {' '.join(name.get('given', []))} {name.get('family', '')}".strip())
    print(json.dumps({k: patient.get(k) for k in ("id", "name", "gender", "birthDate")}, indent=2))
    if args.no_submit:
        return

    body = json.loads(SAMPLE.read_text())
    body.pop("patient")
    body["patientReference"] = f"Patient/{patient['id']}"
    with httpx.Client(base_url=args.api, timeout=10) as api:
        tok = api.post("/auth/token", data={"username": args.user, "password": args.password})
        tok.raise_for_status()
        headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        r = api.post("/requests", json=body, headers=headers)
        print(f"POST /requests -> {r.status_code} {r.text}")
        r.raise_for_status()
        url = r.headers["Location"]
        for _ in range(60):
            data = api.get(url, headers=headers).json()
            if data["status"] in ("approved", "denied", "failed"):
                break
            time.sleep(1)
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
