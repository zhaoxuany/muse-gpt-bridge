# Windows 测试恢复手册 (RECOVERY.md)

出问题了先别慌，对照下面一条一条来。每一节都写了“症状 → 原因 → 怎么做”。

## 1. 桥起不来 (start_bridge.bat 一闪而过 / 报错退出)

| 症状 | 原因 | 怎么做 |
|---|---|---|
| 提示 BRIDGE_TOKEN 没设置 | 环境变量没设，或只在某个窗口里临时设了 | 在 PowerShell 里运行一次 `setx BRIDGE_TOKEN "你的随机凭证"`，关掉重开窗口再运行。不要把凭证写进任何文件 |
| 提示找不到 python | Python 没装，或装时没勾 Add to PATH | 重装 Python，记得打勾；或在命令行里确认 `python --version` 能打出版本号 |
| 提示找不到 token / SIWC 拒绝 | 还没授权，或 token 文件不在统一路径 | 先跑 `python siwc_auth.py` 完成授权；确认 token 文件在 `C:\Users\你的用户名\.muse-gpt-bridge\siwc_tokens.json` |
| 端口被占 (8003 已被占用) | 已经有一个桥在跑 | 任务计划可能已经启动了一个实例。先检查任务管理器里有没有 python 进程在跑桥，关掉多余的再启动 |

## 2. 电脑休眠 / 断网 / 重启后怎么恢复

- **数据不会丢**：讨论历史存在 SQLite 文件里，token 文件也在磁盘上，服务停了数据还在。
- **重启 / 开机**：如果注册了开机自启（`register_autostart.ps1`），桥会在开机 30 秒后自己起来；没注册就手动双击 `start_bridge.bat`。
- **睡眠 / 休眠唤醒**：唤醒触发器会尝试重启桥；如果没起来，手动双击 `start_bridge.bat` 就行。
- **断网**：桥进程还在，等网络恢复后下次调用就正常；正在进行的 GPT 调用会记一次失败，讨论本身还在库里，用原来的 discussion 接着聊就行，不用重起炉灶。
- **access_token 过期**：电脑睡了 2 小时再醒，旧 access_token 已过期。桥下次调用前会自动刷新（只要 refresh_token 还在 30 天内），不用你手动重新授权。
- **refresh_token 过期（30 天没用）**：必须重新跑 `python siwc_auth.py` 授权，拿一套新的。旧的那套作废了。

## 3. token 出问题

| 症状 | 原因 | 怎么做 |
|---|---|---|
| 401 token 无效或已过期，且刷新失败 | refresh_token 也过期了，或被撤掉了 | 重新跑 `python siwc_auth.py` 授权 |
| 403 不合资格 (`subscription_sharing_user_not_eligible`) | 账号/应用现在用不了计划用量 | 先停下来，把报错发给 Muse，不要重复试 |
| 429 用量超限 (`subscription_sharing_usage_limit_exceeded`) | Plus 计划用量 5 小时窗口到了上限 | 等几个小时再试，不要反复刷 |
| 怀疑 token 泄露了 | 文件被别人看到了、或不小心发给别人了 | 去 ChatGPT 设置里找到已连接的应用，把授权撤掉，再重新跑 `python siwc_auth.py`。旧 token 撤掉就作废 |

## 4. 断网恢复演示跑不通

- 断网后调用**应该**失败并被捕获，脚本会打印“如预期断网失败并被捕获”。如果断网了居然还能调通，说明网络没断干净（比如还连着手机热点），检查一下再重跑。
- 恢复网络后，第 3 轮用同一份历史续聊；如果第 3 轮说“不记得前面了”，说明上下文没接上，把结果文件发给 Muse。

## 5. 两个桥同时在跑（重复启动）

- 任务计划“At startup”和唤醒触发器可能同时触发。任务设置里已选“如果任务已在运行，不启动新实例”，正常不会重复。
- 如果发现两个 python 进程都在跑桥，关掉一个（留一个），再检查健康接口 `http://127.0.0.1:8003/api/muse-gpt/health` 能不能返回。

## 6. 数据目录不要放 OneDrive / 桌面同步目录

- 桥的数据文件（`data\bridge.sqlite`）如果放在 OneDrive 同步目录里，云同步会和 SQLite 写锁打架，有损坏风险。桥默认放在桥目录下的 `data\`，拷测试包时别把它整个拷进 OneDrive 同步文件夹。

## 7. 还搞不定怎么办

把下面三样一起发给 Muse，不要自己反复试：
1. 报错原文截图或文字。
2. 你正在做的那一步（比如“第 3 步授权”还是“跑测试第 2 轮”）。
3. `siwc_test_result.json`（如果已经生成了）。
**不要发 `siwc_tokens.json`，不要贴 token。**
