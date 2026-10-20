# Windows 新电脑部署

## 1. 准备环境

- Windows x86-64，Python 3.10+（包含 `py` 启动器）、Git、Docker Desktop，
  使用 WSL2 / Linux containers。安装前确认 Docker 能正常运行。
- 自行从 10x Genomics 获取 **Linux Cell Ranger 10.1.0 tar.gz**，遵守其使用条款；
  获取并解压与物种/实验匹配的 10x 参考基因组。原机使用 GRCh38-2024-A，
  不能用于所有物种。仓库不分发这些大文件。
- 原机预算为 Cell Ranger 10 核 / 36 GB、转换 4 线程。Docker 的实际 CPU/内存
  要高于作业预算，还要给同时下载、系统和 loom 留余量；低配机器需调整。
- 数据盘和 Docker 虚拟磁盘都要预留空间。配置最低余量仅为保护线，
  不是整批数据的容量估算；fasterq 的峰值可能达到 SRA 大小约 10 倍。
- SQLite/日志使用本机 `%LOCALAPPDATA%\scpre`（应为 NTFS）。
  Cell Ranger 程序和每个样本的 pipestance 使用 Docker Linux/ext4 卷。
  exFAT 数据盘不能承载 Martian 的 Unix 工作目录。
- 如需更改 Docker 磁盘位置，请在新机开始作业**之前**通过 Docker 设置操作；
  不要复制原机正在使用的 VHD，也不要在健康作业中途重启 Docker/WSL。

## 2. 下载仓库与安装

这是私有仓库，先由仓库所有者给同事开通访问权限。以下路径只是例子，
`E:` 不是强制要求。尽量使用不含空格/中文的仓库与数据路径。

```powershell
git clone https://github.com/zzx93130/sc_preprocessor.git E:\sc_preprocessor\repo
cd E:\sc_preprocessor\repo
.\install-windows.ps1 -DataRoot E:\sc_preprocessor `
  -CellRangerArchive E:\downloads\cellranger-10.1.0.tar.gz `
  -ReferenceDir E:\references\refdata-gex-GRCh38-2024-A `
  -Cores 10 -MemoryGB 36
```

若 PowerShell 限制脚本，先检查脚本内容，按单位策略仅对当前进程允许执行；
不要全局关闭安全策略。安装脚本不会启动队列、自动注册开机任务或覆盖已有
`config.yaml`。发现已有配置/数据库时应核实原部署，不要另起一个调度器。

构建使用 Ubuntu 24.04、SRA Toolkit 3.2.1 和 `varunkas/velocyto:0.17`，
通过已验证的 HTTPS/registry 拉取。镜像标签目前不是 digest 锁定，Python
依赖也是版本下限；若需严格重现，部署后保存镜像 digest 和 `pip freeze`。
下载工具安装包后按官方发布记录核验，不要因为网络失败关闭 TLS 检查。
`-SkipImages` 仅适用于这些镜像和 Cell Ranger 卷已在新机验证可用的情况。
已有非空且无效的 Cell Ranger 卷会报错，脚本不会清空它。

`paths.cellranger_dir` 是主机上的版本/读段判定白名单元数据目录；可执行
程序位于 `scpre-cellranger-10.1.0` Linux 卷。这不是损坏的程序安装。
不要再用旧的交互式 `init` 向导覆盖生成的 Windows 配置。

## 3. 安装 skill

在 Codex 中提出以下请求，让它先检查目标目录，已有 skill 则确认合并：

> 从这个仓库的 skills/sra-cellranger 安装 skill。随后按照
> docs/WINDOWS-DEPLOYMENT.md 配置这台 Windows 电脑；不要启动独立的
> sra_cellranger_pipeline.py 调度器，不要复制原电脑数据或配置。

也可将整个 `skills/sra-cellranger` 目录复制到个人 Codex skills 目录
`%USERPROFILE%\.codex\skills\sra-cellranger`，注意不要覆盖已有个人修改。
skill 是操作说明，不是服务本身；必须同时部署本仓库的 `pipeline`。

## 4. 配置输入并启动

1. 将这批数据自己的 `SraRunTable.csv` 放到数据根目录的 `to_process`。
   必须包含正确的 SRR→生物样本映射；不要复用原机的样本表或例外配置。
2. 审阅生成的 `repo/config.yaml`。默认 `metadata.process_all: true` 会处理
   表中整批；首次试跑应使用仅含一个代表样本的小表。
3. 核对 `concurrency.separate_downloads: true`、下载 1、全预处理 1、
   `download_ahead: 1`、BAM 与 loom 开启、Windows 清理重启关闭。
   大目录、原始输入和归档默认保留，不主动清理历史数据。
4. 若本地已下载 SRA，将文件放入 inbox 并确保其 accession 可被识别。
   `.man` 改后缀不是验证；可疑/续传数据必须做完整性验证。
5. 启动（保持窗口，关闭/重启电脑可能中断服务和容器）：

```powershell
.\start-windows.ps1
```

打开 http://127.0.0.1:9123 ，确认 `/api/state` 反映独立下载/预处理配置。
不要把仪表盘监听地址改成公网；它不是带登录认证的共享服务。
每台电脑只运行一个队列服务。正式关机前先在仪表盘暂停接收新阶段并确认
所有作业安全停止；重启后先检查容器/日志/续跑点，不要直接重复启动样本。

不配置监控自动任务也可执行队列。若需要每四小时检查，在同事自己的 Codex
会话里明确创建一个监控；健康作业不重启，查询超时不等于失败，不连续轮询。

## 5. 结果与验证

结果在 `<DataRoot>\archive\<project>\<sample>`，默认 project 为 `default`。
必须核验矩阵、BAM/BAI、loom、原始 FASTQ ZIP、SHA/CRC/manifest 与日志。
仪表盘 `done` 不等于所有独立科学/结构审计都通过。

额外结构审计示例（将路径/样本替换为本机实际值；不操作运行中的样本）：

```powershell
docker run --rm --entrypoint python `
  --mount "type=bind,source=E:\sc_preprocessor\archive\default,target=/archive,readonly" `
  --mount "type=bind,source=E:\sc_preprocessor\repo\deploy,target=/checks,readonly" `
  scpre-velocyto:latest /checks/validate_outputs.py GSM_EXAMPLE
```

输出逐样本 JSON；缺样本、空目录或任一样本错误都会非零退出。
这个脚本检查 HDF5/MTX 维度、BAM quickcheck/索引查询、loom 层和细胞数量，
不是完整矩阵值或细胞身份一致性证明；它也不核验 FASTQ ZIP。
ZIP 另需完整重算 SHA256、CRC、成员名称/大小与 manifest 对照；不确定的内层
gzip 要读至 EOF 校验。只有通过要求的审计后才清理临时目录，保留失败续跑数据。

样本读段角色应按该研究的元数据、实际内容和白名单证据判定。原机
SRR9985349 的 paired-only 例外仅属于那份数据，不能迁移成普遍规则。

## 6. 测试（不运行真实数据）

```powershell
.\pipeline\.venv\Scripts\python.exe -m pip install -r pipeline\requirements-dev.txt
cd pipeline
.\.venv\Scripts\python.exe -m pytest tests -q
```

新部署仍需一份真实代表样本的端到端验收；离线测试不能代替它。
