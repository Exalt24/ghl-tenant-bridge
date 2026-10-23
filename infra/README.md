# infra

Terraform for running the relay on AWS: one image as a Fargate web service behind a load balancer and a Fargate worker, on RDS Postgres, with every secret in Secrets Manager.

It has never been applied. No AWS account was used for any of it. What exists is a stack that formats, validates, plans offline and passes its own guard tests, and the register is in the Measured and Limits sections below.

## What it builds

| piece | what | why this shape |
|---|---|---|
| `ecs.tf` | One ECS cluster, a `web` service (2 to 6 tasks, CPU autoscaling) and a `worker` service (1 task), both from the relay image. The worker starts `python -m relay.worker`. Read only root filesystem, a writable `/tmp` volume, non root user. | The relay supports a separate worker only on Postgres, and refuses it otherwise (`relay/relay/config.py`). |
| `data.tf` | RDS Postgres 17, encrypted, Multi-AZ, 7 day backups, deletion protection, a final snapshot. The passwords and tokens are ephemeral and reach the database and Secrets Manager through write-only arguments, so they are not in the state. | The tasks get `DATABASE_URL`, `RELAY_INGEST_TOKEN`, `RELAY_ADMIN_TOKEN` and `RELAY_CONFIG` as secrets, never as plain environment values. The worker gets only the first and last. |
| `network.tf` | Three security groups: internet to load balancer, load balancer to tasks on 8000, tasks to database on 5432. Nothing else. | The database accepts the task group only, by group reference, never a CIDR. |
| `alb.tf` | An application load balancer checking `/livez`. With a certificate port 80 redirects to HTTPS; without one the stack refuses to plan unless `allow_plain_http` is true. An optional Route 53 alias. | The ingest token is a bearer header, so plain HTTP is opt in. `/livez` answers without the database, so a database failover does not make every task fail its check together. |

## Run it

You need Terraform 1.11 or later. No AWS account, key or network access is needed for any of this.

```
cd infra/terraform
terraform init -backend=false
terraform validate
terraform test
AWS_ACCESS_KEY_ID=offline AWS_SECRET_ACCESS_KEY=offline terraform plan -var-file=plan.tfvars.example
python tests/mutate.py
```

`plan.tfvars.example` holds made-up VPC, subnet and image identifiers and sets `offline_plan = true`, which makes the provider skip its credential and account lookups. `offline_plan` is false by default, so a real apply checks credentials as usual. Copy `backend.tf.example` to `backend.tf` before a real apply to keep the state in an encrypted S3 bucket with native locking.

## First apply and day two

The stack creates the image repository and the services in one configuration, and a task cannot start until its image exists, so the first apply has a fixed order:

1. `terraform apply -target=aws_ecr_repository.relay`
2. Build the image from `relay/Dockerfile` for linux/amd64 and push it to the `ecr_repository_url` output with the tag you will pass as `image_tag`.
3. Put the relay's JSON config in the `relay_config_secret_arn` secret, and create one secret per vendor key the config names (a Slack URL, a Meta token, a Sheets key). Give them to the stack in `vendor_secret_arns`, a map from the environment variable name to the secret's ARN.
4. `terraform apply`

A task reads its secrets once, when it starts. After you change any secret, including the config, run `aws ecs update-service --cluster <cluster_name> --service <name> --force-new-deployment` for both services.

The database password and the tokens are generated fresh on every run and are only sent when `secrets_version` changes. If an apply fails after the database exists, the next run would make a new password that the database never receives, so run it with `-var secrets_version=<previous + 1>` and both sides are sent together. To rotate everything on purpose, raise `secrets_version` the same way.

## Measured

One full local pass on 2026-10-24 with Terraform 1.16.6, AWS provider 6.70.0 and random provider 3.9.1:

- `terraform fmt -check` clean, `terraform validate` succeeds.
- `terraform plan` offline: 40 resources to add, 0 to change, 0 to destroy.
- `terraform test`: 17 runs pass, covering encryption and privacy of the database, its ingress rule, task isolation, secrets handling, the worker's narrower secret set and its own execution role, write-only passwords, the writable `/tmp` volume, the `/livez` checks, the HTTPS rules and the autoscaling and image retention settings.
- `python tests/mutate.py`: 32 of 32 mutants caught. Each copy of the stack breaks one guard (for example `storage_encrypted = false`, a CIDR on the database rule, a secret in the environment block, the worker given the admin token, a service no longer waiting for the database) and the named test run, or the source check in `tests/check_dependencies.py`, must fail.
- A container check against the real relay image: with `--read-only` and no writable `/tmp` the container exits with `No usable temporary directory found`, and with `--tmpfs /tmp` and `HOME=/tmp` it answers `/health` with no errors in its log. That is why the stack mounts a `/tmp` volume and sets `HOME`.

## Limits

- Never applied. The plan uses made-up identifiers, so it proves the configuration is consistent and the guards hold, not that AWS accepts it in a real account (quotas, subnet routing, a NAT path for the vendor calls and image pull permissions are untested). The `/tmp` volume was tested as a Docker tmpfs, not as a Fargate volume, so its permissions for the non root user on Fargate are unverified.
- The container image is not pushed anywhere. Build it from `relay/Dockerfile`, push it to the ECR repository the stack outputs, then set `container_image`.
- The write-only arguments keep the generated values out of the state. `RELAY_CONFIG` is a plain secret version set by hand with the AWS CLI.
- No cost estimate. Multi-AZ RDS, a load balancer and the NAT path the tasks need are the items that cost money while idle.
- Egress allows HTTPS to anywhere from the tasks, and there are no VPC endpoints, so AWS API traffic goes through the NAT. There are no load balancer access logs, no WAF and no KMS key on the log group.
- The connection string says `sslmode=require`, which encrypts the connection and does not check the server's certificate. Checking it (`verify-full`) needs the RDS CA bundle in the image.
- The relay connects as the database master user `relay`, with no rotation and no separate low privilege role.
- The worker has no health check, so a worker that hangs without exiting is not restarted.
- Tasks are set to linux/amd64. An image built on an ARM host will not start.
- Teardown takes two steps: apply once with `deletion_protection = false`, then destroy. Change `final_snapshot_suffix` for each create and destroy cycle, because a snapshot name can be used once.
- The relay stays unverified against any real vendor, as its own README says. This stack does not change that.
