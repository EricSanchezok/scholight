# Retired deployment reference

The files in `workflows/` are historical recovery references, outside GitHub Actions.
They must not be copied back into `.github/workflows/` or run against production.
Normal publication and deployment use the manual personal-account workflows in
account `669409472143`, region `ap-south-2`. No push, CI-completion event, tag or
GitHub Release publishes production images or deploys applications.

Historical templates and full product implementations are retained for inspection.
Recover compatible application images inside the current personal environment;
restoring old compute requires a separately reviewed reconstruction plan. Never
restore the shared production database from an old snapshot during application rollback.
