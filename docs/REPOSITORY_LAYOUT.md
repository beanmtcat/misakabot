# Repository layout

```text
apps/       Standalone browser-facing applications and services
docs/       Design notes and operational documentation
plugins/    Independently installable integrations
```

`apps/bot/` contains the MisakaBot package, tests, maintenance scripts, and its Docker build definition. The root `compose.yaml` and `deploy.sh` orchestrate applications under `apps/`.
