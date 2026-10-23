# infra

Terraform for running the relay on AWS: one image as a Fargate web service behind a load balancer and a Fargate worker, on RDS Postgres, with every secret in Secrets Manager.

It has never been applied. No AWS account was used for any of it. What exists is a stack that formats, validates, plans offline and passes its own guard tests, and the register is in the Measured and Limits sections below.

## What it builds

| piece | what | why this shape |
|---|---|---|
| `ecs.tf` | One ECS cluster, a `web` service (2 to 6 tasks, CPU autoscaling) and a `worker` service (1 task), both from the relay image. The worker starts `python -m relay.worker`. Read only root filesystem, a `/tmp` volume, non root user. | The relay supports a separate worker only on Postgres, and refuses it otherwise (`relay/relay/config.py`). |
| `data.tf` | RDS Postgres 17, encrypted, Multi-AZ, 7 day backups, deletion protection, a final snapshot. The passwords and tokens are ephemeral and reach the database and Secrets Manager through write-only arguments, so they are not in the state. | The tasks get `DATABASE_URL`, `RELAY_INGEST_TOKEN`, `RELAY_ADMIN_TOKEN` and `RELAY_CONFIG` as secrets, never as plain environment values. The worker gets `DATABASE_URL`, `RELAY_CONFIG` and any vendor secrets, and never the two tokens. |
| `network.tf` | Three security groups: internet to load balancer, load balancer to tasks on 8000, tasks to database on 5432. Nothing else. | The database accepts the task group only, by group reference, never a CIDR. |
| `alb.tf` | An application load balancer checking `/livez`. With a certificate port 80 redirects to HTTPS; without one the stack refuses to plan unless `allow_plain_http` is true. An optional Route 53 alias. | The ingest token is a bearer header, so plain HTTP is opt in. `/livez` answers without the database, so a database failover does not make every task fail its check together. |

## Run the checks

You need Terraform 1.11 or later, tflint with its AWS ruleset (`tflint --init` fetches it), Python 3, and Docker for the last command. No AWS account, key or network access to AWS is needed for any of this.

```
cd infra/terraform
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
tflint --init && tflint
terraform test
AWS_ACCESS_KEY_ID=offline AWS_SECRET_ACCESS_KEY=offline terraform plan -var-file=plan.tfvars.example
python tests/check_dependencies.py
python tests/check_wiring.py
python tests/mutate.py
python tests/container_readonly_check.py
```

In PowerShell set the two variables first: `$env:AWS_ACCESS_KEY_ID = 'offline'; $env:AWS_SECRET_ACCESS_KEY = 'offline'`.

`plan.tfvars.example` holds made-up VPC and subnet identifiers and sets `offline_plan = true`, which makes the provider skip its credential and account lookups. Never use that file for a real apply: `offline_plan` is false by default, so a real apply checks credentials as usual. Copy `backend.tf.example` to `backend.tf` before a real apply to keep the state in an encrypted S3 bucket with native locking.

## First apply and day two

The stack creates the image repository and the services in one configuration, and a task cannot start until its image exists, so the first apply has a fixed order:

1. `terraform apply -target=aws_ecr_repository.relay -var-file=<your values>`
2. Build the image with `docker build --platform linux/amd64 -t <ecr_repository_url>:<image_tag> relay` (the build context is the `relay` folder) and push it to the `ecr_repository_url` output with the tag you will pass as `image_tag`.
3. Create one secret per vendor key the relay's config names (a Slack URL, a Meta token, a Sheets key), each a plain Secrets Manager ARN with no `:json-key` suffix, and pass them in `vendor_secret_arns`, a map from the environment variable name to the ARN. Set `relay_live = "1"`: the starting config is `{}`, which starts either way, but once the real config names vendor URLs the relay refuses any that is not local unless `RELAY_LIVE` is 1, and every task exits at start.
4. `terraform apply`. It waits for both services to reach a steady state, so a failed first start fails the apply.
5. Put the relay's JSON config in the `relay_config_secret_arn` secret with `aws secretsmanager put-secret-value`, then run `aws ecs update-service --cluster <cluster_name> --service <name> --force-new-deployment` for both services.

A task reads its secrets once, when it starts, so after any secret changes, including the config, force a new deployment of both services.

The database password and the tokens are generated fresh on every run and are only sent when `secrets_version` changes. Three consequences:

- If an apply fails after the database exists, the next run would make a new password that the database never receives. Run it with `-var secrets_version=<previous + 1>` and both sides are sent together.
- To rotate everything on purpose, raise `secrets_version`, apply, then force a new deployment of both services at once. The database stops accepting the old password when the apply finishes (RDS applies a master password change as soon as possible), so until the redeploy every new database connection from a running task fails authentication and `/health` returns 503, although `/livez` stays up and ECS does not restart them. A rotation with no gap needs a second database user, which this stack does not create. HighLevel keeps the old ingest token, so update it in the same window. The relay accepts a second token through `RELAY_INGEST_TOKEN_PREVIOUS` for a gradual change, and this stack does not wire it.
- Replacing the database instance needs a version bump too, because a write-only value never shows a difference and `DATABASE_URL` would keep the old host.

## Measured

One full local pass on 2026-10-24 with Terraform 1.16.6, AWS provider 6.70.0, random provider 3.9.1 and tflint 0.65.0 with the AWS ruleset 0.50.0:

- `terraform fmt -check` clean, `terraform validate` succeeds, `tflint` reports nothing.
- `terraform plan` offline: 40 resources to add, 0 to change, 0 to destroy.
- `terraform test`: 28 runs pass, covering encryption and privacy of the database, its ingress rule, task isolation, secrets handling, the worker's narrower secret set and its own execution role, write-only passwords, the shape of the `/tmp` volume and `HOME` in the task definition, the `/livez` checks, the HTTPS rules, vendor secrets, the variable checks, steady-state waiting and the autoscaling and image retention settings.
- `python tests/mutate.py`: every mutant caught (the count is printed on the last line). Each copy of the stack breaks one guard (for example `storage_encrypted = false`, a CIDR on the database rule, a secret in the environment block, the worker given the admin token, a service no longer waiting for the database) and the named test run, or one of the source checks (`tests/check_dependencies.py` or `tests/check_wiring.py`), must fail.
- `python tests/container_readonly_check.py`, against the real relay image on a read only root: with a read only `/tmp` the container exits with `No usable temporary directory found`, with the image's own `/tmp` volume (mode 1777) or a tmpfs it answers `/health`. The `VOLUME ["/tmp"]` line in the Dockerfile is there because ECS copies an image directory's contents and permissions into a task volume only for a path named in a `VOLUME` directive; otherwise the volume is root owned 0755.

## Limits

- Never applied. The plan uses made-up identifiers, so it proves the configuration is consistent and the guards hold, not that AWS accepts it in a real account (quotas, subnet routing, a NAT path for the vendor calls and image pull permissions are untested). The container check uses Docker volumes and a tmpfs, not a Fargate volume, so whether the non root user can write to `/tmp` on Fargate is unverified until a task runs.
- The container image is not pushed anywhere.
- The write-only arguments keep the generated values out of the state. `RELAY_CONFIG` is a plain secret version set by hand with the AWS CLI.
- No cost estimate. Multi-AZ RDS, a load balancer and the NAT path the tasks need are the items that cost money while idle.
- Egress allows HTTPS to anywhere from the tasks, and there are no VPC endpoints, so AWS API traffic goes through the NAT. There are no load balancer access logs, no WAF and no KMS key on the log group. Vendor secrets must use the default AWS managed key: a customer managed key also needs `kms:Decrypt` on the execution roles, which this stack does not add.
- `GET /health` and the two `/admin` routes are reachable from the internet through the load balancer; `/health` needs no token and returns event and delivery counts, and the admin routes sit behind a 40 character random bearer token. Add a listener rule or a WAF rule if that matters.
- The two subnet lists are only checked for count, not for being in different availability zones.
- The connection string says `sslmode=require`, which encrypts the connection and does not check the server's certificate. Checking it (`verify-full`) needs the RDS CA bundle in the image.
- The relay connects as the database master user `relay`, with no rotation and no separate low privilege role.
- The worker has no health check, so a worker that hangs without exiting is not restarted.
- Tasks are set to linux/amd64. An image built on an ARM host will not start.
- Teardown takes two steps: apply once with `deletion_protection = false` and `ecr_force_delete = true` (the repository holds the pushed image), then destroy. Change `final_snapshot_suffix` for each create and destroy cycle, because a snapshot name can be used once.
- The relay stays unverified against any real vendor, as its own README says. This stack does not change that.
