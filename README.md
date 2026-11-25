# SRA / GEO single-cell preprocessing

Windows Docker workflow and reusable Codex skill, packaged from the production
deployment on 2026-10-20. Downloads can overlap preprocessing, but only one sample
occupies the entire conversion → Cell Ranger → loom → archive → verification →
cleanup lifecycle. Dashboard: http://127.0.0.1:9123 (local access only).

原有 `skills/sra-auto-pipeline` 保留为历史内容；新 Windows 部署请使用
`sra-cellranger`，不要让两个 skill 启动两个调度器。

This repository is private. The owner must grant colleagues access before they
can clone it. No public/open-source license is asserted here; third-party tools
retain their respective licensing and must be obtained separately.

## Verification scope

The Python regression tests exercise queue scheduling, recovery, input handling
and download fallback without running a real sequencing batch. New installer
checks are isolated/mocked: they do not prove an end-to-end installation on every
Windows machine. Run one small, representative sample and audit its outputs on
the colleague's computer before submitting the full study.
