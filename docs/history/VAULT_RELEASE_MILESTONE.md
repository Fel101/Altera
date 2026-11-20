# Deferred Altera password vault

The password vault is deferred on the local branch `codex/password-vault`. That branch preserves the full implementation, test history, verification guides and original handoff through `f884d0c`. The ordinary release starts from the pre-vault commit `f62e2c5` and carries forward the ordinary app features.

The ordinary app keeps encrypted backup archives and refuses to open an instance marked for the vault or an unfinished vault migration. For development or recovery work, use a separate checkout of `codex/password-vault` with a disposable instance directory. Native encrypted storage, migration and real-device acceptance remain open.
