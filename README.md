# android-wechat-recovery

安卓手机**微信聊天记录数据库损坏**后的全流程引导式恢复 Skill（适用于 WorkBuddy / CodeBuddy 等支持 Skill 的智能体）。

当微信检测到加密数据库 `EnMicroMsg.db` 内部出现坏页时，会把整个库搬到 `MicroMsg/<hash>/corrupted/` 并新建空库，导致聊天记录"消失"，但数据其实还在。本 Skill 从零引导用户完成：**开启开发者模式 → USB 调试 → 品牌确认 → 安全备份 → 一次性拉取到电脑 → 电脑端解密修复 → 完整性门禁验证 → 重建备份包 → 一次性回传恢复 → 用户确认成功 → 双端清理**。

> 实测案例：Xiaomi 17 Pro / WeChat 8.0.76，损坏库 3.1 GB，修复并恢复约 151 万条聊天记录。

---

## 适用症状（三条同时成立即高度匹配）

1. 微信聊天记录突然全部消失，只剩最近几条或完全空白；
2. 微信自带「设置 → 帮助与反馈 → 修复聊天记录」无效，或修复后数据又消失；
3. 手机「存储空间」里微信仍占用几十 GB，数据并没有真的被删。

## 核心难点

不能简单把 `corrupted/` 里的库搬回去——微信一开机又会检测到损坏再次清空。必须在**电脑上把库修好**（剔除坏页、重建干净加密库），通过 `integrity_check` 门禁验证后再回传。

## 设计原则（已在脚本中硬编码）

| 原则 | 含义 |
|------|------|
| 电脑端闭环 | 手机只做两次大文件 I/O：拉 1 次 + 推 1 次，其余全在本地完成 |
| 先验后传 | 修好的库必须通过 `integrity_check` 门禁才允许回传 |
| 空间先算后做 | 写入前估算需求、扫描各磁盘，不足则提示切换工作盘 |
| 二进制必须 exec-out | 拉取用 `adb exec-out`，避免 PTY 转换损坏二进制 |
| 原始数据只读 | `corrupted/` 在流程结束前不删，它是唯一数据源 |
| 清理需双确认 | 清理前先列清单并获明确确认，且在确认恢复成功之后 |
| 长任务报进度 | 拉取 / 手术 / 推送等任务分阶段打印进度 |

---

## 目录结构

```
android-wechat-recovery/
├── SKILL.md                      # 主技能（10 阶段 + 6 交互门 + 7 总则）
├── README.md
├── LICENSE
├── .gitignore
├── references/                   # 6 份深度参考文档
│   ├── 01-device-setup.md        # 各品牌开发者模式 / USB 调试 / ADB 排障
│   ├── 02-brand-playbooks.md     # 品牌 → 回传方案矩阵
│   ├── 03-key-and-repair.md      # SQLCipher 密钥候选矩阵、修复原理
│   ├── 04-miui-backup-format.md  # MIUI 备份格式、tar 手术、descript.xml
│   ├── 05-troubleshooting.md     # 高频问题速查
│   └── 06-cleanup-checklist.md   # 双端清理清单与安全红线
└── scripts/                      # 11 个可执行脚本
    ├── preflight.py              # S0 环境 / 依赖 / 磁盘预检
    ├── device_probe.py           # S2 设备探测与品牌分支
    ├── diagnose_wechat.py        # S3 损坏诊断与空间预判
    ├── pull_wechat_data.py       # S4 一次性安全拉取
    ├── get_wechat_key.py         # S5 密钥推导与验证
    ├── recover_database.py       # S6 分批修复到干净加密库
    ├── update_sm_salt.py         # S6-b 同步 .sm salt
    ├── verify_gate.py            # S7 回传前硬性验证门禁
    ├── miui_backup_surgery.py    # S8-a 本地 tar 手术
    ├── make_descript.py          # S8-b 生成 descript.xml
    └── cleanup.py                # S10 双端清理（dry-run + 确认双保险）
```

## 安装到 WorkBuddy

```bash
# 方式一：直接克隆到技能目录
git clone <本仓库地址> ~/.workbuddy/skills/android-wechat-recovery

# 方式二：手动放置
# 把整个 android-wechat-recovery/ 目录复制到 ~/.workbuddy/skills/ 下即可
```

放置后，在对话中描述症状（如"微信聊天记录丢失、微信修复无效"）即可触发本 Skill。

## 前置依赖

- 已安装 ADB（`platform-tools`）并加入 PATH
- Python 3.10+；修复阶段需要可用的 `sqlcipher3`（或 `pysqlcipher3`）
- 一台已开启 USB 调试的安卓手机 + 原装数据线

## 快速演示（以 S10 清理为例）

```bash
# 先看清单（不删任何东西）
python scripts/cleanup.py --workdir <工作目录> --hash <32位hash> --dry-run

# 用户确认清单无误后执行（不含 corrupted/）
python scripts/cleanup.py --workdir <工作目录> --hash <hash> \
    --bak-orig 20260801_140743 --bak-new 20260801_153000 \
    --archive "D:\聊天数据恢复" --confirm

# 用户最终确认微信稳定后，再单独删手机 corrupted/
python scripts/cleanup.py --workdir <工作目录> --hash <hash> \
    --confirm --delete-corrupted
```

## 免责声明

本工具用于**恢复用户自己的**微信聊天记录。操作涉及对手机存储的读写与备份修改，请务必先按流程完成归档（回滚点）并确认数据恢复成功后再清理。因误操作、未确认即清理、或微信版本差异导致的数据问题，作者不承担责任。

## License

[MIT](LICENSE)
