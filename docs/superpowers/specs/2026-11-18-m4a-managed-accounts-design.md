# 里程碑 4a · 非飞书账号与受控外部账号 — 设计

> 状态:设计已逐段确认(2026-11-18),待评审本文档后写实现计划。
> 范围:PRD 里程碑 4 的前半(F-5.1.2 多身份源 + 受控外部合作方)。里程碑 4 后半"账号自动供给/回收"为 4b,另行设计。

## 1. 背景与目标

现在所有自动停用都依赖**飞书在职状态**:离职事件、每日对账、权限核对都只看 id 以 `ou_` 开头的账号(`casdoor_admin.list_approved_feishu_members`、`access_report.check_feishu`)。飞书以外的账号一旦拿到准入,就永远不会被自动停用。

加一个身份源(邮箱/密码、其他 OIDC/SAML)在 Casdoor 里只是配置,应用不用改。PRD 里"加身份源不改应用"这条 Casdoor 已经满足。里程碑 4a 要做的是治理层:**每个非飞书账号都必须有回收依据**,没有依据的不许批准。

### 1.1 现有风险(本设计一并修复,待实测确认)

飞书首次扫码要能自动注册,Casdoor 应用必须开着 `enableSignUp=true`。这很可能意味着 `/signup` 页也开着,陌生人能自助注册一个邮箱密码账号。注册时,signup webhook(`webhook.handle`)和 `/request-access`(`app.request_access`)都会给他发审批卡片,两处都不检查账号来源。卡片上的姓名和邮箱是他自己填的,审批人分辨不出来。第 4.4 节的闸门会补上这个洞。

## 2. 使用人群与回收依据

| 账号类型 | `evauth_kind` | 回收依据 | 实现 |
|---|---|---|---|
| 飞书员工(含备用密码登录) | 不写 | 飞书在职状态 | 挂在同一个 Casdoor 账号上:现有离职停用会停整个账号,所有登录方式一起失效 |
| 外部合作方 | `partner` | 负责人 + 到期日 | 负责人离职或到期未续,就停用 |
| 没飞书的内部员工 | `staff` | 负责人 + 到期日 | 同上。以后接 HR 系统再单独加 |
| 机器/服务账号 | `service` | 负责人 + 到期日 | 同上,负责人就是账号的主人 |

第一版只支持**邮箱/密码**这一种非飞书登录方式。外部 OIDC/SAML 有具体对接方时再加,治理层不用改。

## 3. 账号模型

当前状态记在 Casdoor 用户的 `properties` 上,Casdoor 是唯一的数据来源:每日备份已经覆盖,核对报表一次 `get-users` 就能读全。键名统一加 `evauth_` 前缀,避免和 Casdoor 自己存的 `oauth_Lark_*` 冲突。

| 键 | 含义 | 格式 |
|---|---|---|
| `evauth_kind` | 账号类型 | `partner` / `staff` / `service` |
| `evauth_sponsor` | 负责人 | 飞书 open_id,必须以 `ou_` 开头 |
| `evauth_expires` | 到期日,按 `SCHEDULER_TIMEZONE`(默认 Asia/Shanghai)当天有效 | `YYYY-MM-DD` |
| `evauth_confirmed_at` | 负责人确认时间;空 = 还没确认 | ISO 8601 |
| `evauth_rejected_at` | 负责人拒绝时间 | ISO 8601 |
| `evauth_reminded` | 已发的续期提醒,`<到期日>:<提前天数>`;续期后到期日变了,提醒自然重新开始 | 如 `2027-02-01:14` |
| `evauth_sponsor_pending` | 换负责人时,等待确认的新负责人(4.6) | 飞书 open_id |

**规则**(`accounts.py`,纯函数,传入 `today` 以便测试):

- `days_left = evauth_expires - today`(天)。`days_left >= 0` 有效,`< 0` 已过期。
- **合格**(可以被批准、可以触发审批卡片)= 满足以下任一条:
  - id 以 `ou_` 开头(飞书账号,沿用现有规则);
  - 同时满足:`evauth_kind` 合法;`evauth_sponsor` 以 `ou_` 开头;`evauth_expires` 能解析;`evauth_confirmed_at` 非空;`evauth_rejected_at` 为空;没过期。
- 不合格原因:`unregistered`(没有 `evauth_kind`)、`invalid_metadata`、`unconfirmed`、`rejected`、`expired`。
- 账号是否停用(`isForbidden`)另外检查,和现在一样。

**宁可错停**:账号登记了 `evauth_kind`,但负责人或到期日缺失、格式不对,扫描时**按已到期处理并停用**。这和飞书接口临时出错时"不误停"的策略相反,因为这批账号没有别的回收途径。
**没登记**(没有 `evauth_kind`)的非飞书账号**不会被自动停用**(Casdoor 里可能有管理员测试号、存量服务账号),只是过不了闸门,并在核对报表里报出来。存量账号用 `accounts register` 纳入管理(见 4.1)。

**有效期上限**:建号、续期、纳管时都要求 `evauth_expires - today <= ACCOUNT_MAX_DAYS`(默认 180)。

## 4. 流程

### 4.1 建号与负责人确认(只能邀请,不开放注册)

```
python -m even_auth_gov.accounts create --kind partner --name 张三 --email a@x.com \
       --sponsor <负责人飞书邮箱> --expires 2027-02-01
```

1. 校验:负责人能按邮箱查到,id 以 `ou_` 开头,而且没停用;到期日不超过上限;这个邮箱在 org 里没被占用。任何一项不通过就退出(码 2),什么都不建。
2. 建 Casdoor 账号:
   - id = `x_` + 16 位随机十六进制;`name` 是否另取取决于实测 S1;
   - `isForbidden=true`;
   - 写入 `evauth_kind/sponsor/expires`;
   - 随机生成 20 位初始密码,**命令行只打印这一次**,不落盘,由管理员线下交给对方;
   - 人员账号首次登录强制改密码(实测 S2),`service` 不强制。
3. 给负责人发**确认卡片**:"管理员为 张三(a@x.com,外部合作方)建了账号,登记你为负责人,有效期至 2027-02-01",下面是【确认负责】和【拒绝】两个按钮。
   - 确认 → 写 `evauth_confirmed_at`。命令行新建的账号此时启用;如果确认时已经过了到期日,就不启用,并提示管理员续期。用 `register` 纳管的账号,确认不改启停状态;
   - 拒绝 → 写 `evauth_rejected_at`,账号保持停用;
   - 点按钮时按 Casdoor 实时数据核对,**操作人必须是当前 `evauth_sponsor`**。
   - 卡片发送失败:命令行提示失败,管理员可以用 `accounts resend-confirm --user` 重发。
4. 账号启用后,对方登录应用,走现有的应用审批。审批卡片上多一行:`外部合作方 · 负责人 李四 · 有效期至 2027-02-01`。

**纳管存量账号**:`accounts register --user <邮箱或 id> --kind --sponsor --expires` 只写属性并发确认卡片,**不改账号的启停状态,也不动已有准入**。负责人确认之前,这个账号过不了闸门,也就不能申请新应用。

**服务账号**:流程同上,`--kind service`,不强制改密码。用 password grant 取 token;对接应用要在 Casdoor 里允许 password grant,写进接入说明。

### 4.2 飞书员工的备用登录

`accounts set-password --user <飞书邮箱>`:给这个人的飞书账号(`ou_`)设随机初始密码,强制首次修改,命令行只打印一次。不新建账号,现有离职停用自然覆盖。对受管账号使用同一个命令 = 重置密码。

### 4.3 到期、续期提醒、续期

在现有的**每小时待批扫描**(`SSO_PENDING_SWEEP_CRON`)里加一步账号扫描,每次多调一次 `get-users`。`get-users` 失败就整步跳过、记警告,下一小时再试。

对每个登记了 `evauth_kind`、没有停用的账号:

- **已过期或数据异常** → `offboard.apply(user_id, name, reason="account_expired" | "account_invalid")`:停账号、清准入组、撤角色、审批记录置为 disabled,失败重试并告警(PRD F-5.4.4)。然后通知负责人。
- **负责人在 Casdoor 里查不到或已停用** → `offboard.apply(..., reason="sponsor_left")`。这是负责人离职连带停用的第二道保险(第一道见 4.5)。
- **到期前提醒**:取 `ACCOUNT_REMIND_DAYS`(默认 `14,3`)里满足 `days_left <= d` 的**最小** d。如果 `evauth_reminded` 记录的是本次到期日,而且记录的档位 ≤ d,就不发;否则给负责人发**续期卡片**,然后把 `evauth_reminded` 写成 `<到期日>:<d>`。这样只发一张,不会同时发 14 天和 3 天两张;剩 2 天时才首次扫到,也只发 3 天那档。
  - 卡片内容:"张三 的账号将于 2027-02-01 到期",下面是【续期 90 天】和【不再续期】两个按钮。
  - 续期:`新到期日 = min(max(today, 原到期日) + ACCOUNT_RENEW_DAYS, today + ACCOUNT_MAX_DAYS)`。
  - 不再续期:不再提醒(`evauth_reminded` 记为最小一档),到期按时停用。
  - 只认当前负责人。管理员用 `accounts renew --user [--expires]` 续期。
- 发卡失败不写 `evauth_reminded`,下一小时重试。

**到期后再续期**:`accounts renew` 写新到期日并启用账号。准入组和角色在停用时已经清掉,要重新走各应用的审批。现在审批库对 `disabled` 记录不允许重新申请(`approval_store.mark_pending` 只放行无记录或 `denied`),所以重新启用时,把这个账号原因为 `account_expired / account_invalid / sponsor_left` 的 disabled 记录改成新状态 `expired`,并把 `expired` 加进允许重新申请的状态。

### 4.4 闸门(同时修复 1.1 的风险)

三处按 Casdoor 实时数据判断账号是否合格:

| 位置 | 不合格时 |
|---|---|
| `webhook.handle`(signup / add-user) | 不建待批、不发卡片,记 info 日志,返回 200。说明:命令行建号可能触发 `add-user` webhook(实测 S6),没有闸门的话,负责人还没确认就会给审批人发卡片 |
| `/request-access` | 返回 403 `{"error": "账号未登记、未确认或已过期,请联系管理员"}`,不建待批 |
| `decision.handle` 批准 | 按拒绝处理:`mark_denied(by="system")`,给 `mark_denied` 加一个可选的 `reason` 参数,记下 `ineligible:<原因>`,并告诉点卡片的人。防止从别处漏进来的待批记录被批准 |

飞书账号(`ou_`)的行为完全不变。

### 4.5 负责人离职

- **事件触发**:`offboard.apply` 对一个飞书用户停用**成功**后,调用 `account_lifecycle.cascade_sponsor(open_id, client)`,逐个停用 `evauth_sponsor == open_id` 的启用账号(`reason="sponsor_left"`)。然后给 `APPROVER_FEISHU_ID` 发一条汇总:"李四 离职,名下 N 个账号已停用:……;需要保留的请改负责人后续期"。负责人一定是 `ou_` 账号,受管账号不能当负责人,所以不会循环触发。连带停用中途失败的,由 4.3 的每小时扫描兜底。
- **对账兜底**:每日对账(`reconcile.run`)的检查范围,加上所有登记了 `evauth_kind`、而且启用中的账号的负责人。负责人可能自己不在任何准入组里,现在对账查不到他。加上之后,他离职会被发现、被 `offboard.apply`,进而触发连带停用。

### 4.6 换负责人

`accounts transfer --user ... --sponsor <新负责人邮箱>`:校验新负责人(同 4.1),给他发确认卡片,写一个待确认的 `evauth_sponsor_pending`。新负责人确认后,`evauth_sponsor` 换成新负责人,`evauth_confirmed_at` 更新。旧负责人在确认之前一直有效。如果账号此前因"负责人离职"被停用,换负责人确认后还要 `accounts renew` 才会重新启用。

### 4.7 `accounts list`

列出受管账号(CSV):姓名、邮箱、id、类型、负责人(姓名 + open_id)、到期日、剩余天数、状态(待确认 / 已拒绝 / 有效 / 已停用)。

## 5. 模块与改动

**新增:**

- `even_auth_gov/accounts.py`:
  - 账号模型和规则(纯函数):属性解析、是否合格、该发哪次提醒、续期天数计算;
  - 命令行子命令:`create`、`register`、`set-password`、`renew`、`transfer`、`resend-confirm`、`list`。
- `even_auth_gov/account_lifecycle.py`:
  - `sweep(client)`:到期、数据异常、负责人停用、续期提醒;
  - `cascade_sponsor(open_id, client)`;
  - `handle_action(action, operator_id, user_id, client)`:处理确认、拒绝、续期、不再续期四种按钮;
  - `log_event(...)`:写入 `account_events.jsonl`。

**改动:**

| 模块 | 改动 |
|---|---|
| `casdoor_admin.py` | `create_user`、`set_properties`(读出整个对象 → 合并 `properties` → 写回,持 `_GROUP_LOCK`)、`set_password`、`enable_user` |
| `webhook.py` | 闸门(4.4) |
| `app.py` | `/request-access` 闸门;`_send_card` 对非 `ou_` 账号查出类型、负责人姓名、到期日,作为身份行传给卡片 |
| `decision.py` | 批准前检查是否合格(4.4) |
| `offboard.py` | 停用成功后调用 `cascade_sponsor`,出错只记日志、不影响离职结果 |
| `reconcile.py` | 每日对账的检查范围加入负责人;`run()` 在 `sweep_pending` 之后也执行一次 `account_lifecycle.sweep(client)` |
| `scheduler.py` | 每小时任务在 `sweep_pending` 之后建 Casdoor client,执行 `account_lifecycle.sweep`。它和每日对账共用 `account_lifecycle` 内部的一把锁,同一时刻只会有一个扫描在跑 |
| `approval_store.py` | 新状态 `expired`,允许重新申请;`reopen_after_renewal(open_id)` |
| `feishu.py` | 确认卡片、续期卡片;审批卡片加身份行 |
| `feishu_ws.py` | 四种 `acct_*` 按钮交给 `account_lifecycle.handle_action` |
| `access_report.py` | 视图加 `kind / sponsor / expires` 三列;新增异常(见下) |
| `audit_export.py` | `--accounts` 导出 `account_events.jsonl` |
| `settings.py` | `account_max_days()`、`account_renew_days()`、`account_remind_days()` |
| `deploy/casdoor/backup.sh` | 文件存在时一并备份 `account_events.jsonl` |

**核对报表新增异常:**

| 严重度 | kind | 条件 |
|---|---|---|
| 高 | `unregistered_with_access` | 非 `ou_`、没有 `evauth_kind`、启用中,而且有准入组或角色 |
| 高 | `expired_but_enabled` | 已过期或数据异常,但还启用着 |
| 高 | `sponsor_gone_but_enabled` | 负责人在 Casdoor 里查不到或已停用,但账号还启用着 |
| 中 | `unregistered_account` | 非 `ou_`、没有 `evauth_kind`、启用中、没有准入 |
| 中 | `invalid_account_metadata` | 登记了但属性不完整或格式不对 |
| 提示 | `expiring_soon` | 有效,`max(ACCOUNT_REMIND_DAYS)` 天内到期 |
| 提示 | `awaiting_sponsor` | 没确认也没拒绝,超过 7 天 |

**审计**:账号的当前状态在 Casdoor。变更历史追加写到 `account_events.jsonl`(`ACCOUNT_EVENTS_FILE`,默认和 `APPROVAL_STORE_FILE` 在同一目录),每行一个 JSON:`ts, event, user_id, name, email, kind, sponsor, expires, actor, detail`。事件包括:`created / registered / confirmed / rejected / renewed / no_renew / transfer_requested / transferred / password_set / expired / invalid / sponsor_left`。这个文件丢了只会丢历史,账号的回收依据还在。

**新增配置**(`gov.env.example`):`ACCOUNT_MAX_DAYS=180`、`ACCOUNT_RENEW_DAYS=90`、`ACCOUNT_REMIND_DAYS=14,3`、`ACCOUNT_EVENTS_FILE`(可选)。

**接入应用:不用改。** 对应用来说,外部账号和员工账号一样,都是"组 + 角色"。如果以后有应用想区分外部人员,可以把 `evauth_kind` 放进 token,这一版先不做。

## 6. 实测(实现前,临时 Casdoor v4.29.0)

| # | 要确认 | 如果不支持 |
|---|---|---|
| S1 | 用户名是随机串的账号,能不能用邮箱 + 密码登录(网页登录和 password grant 两条路) | 先停下来和用户商量;备选:`name` 取邮箱前缀并去重,登录用 `name` |
| S2 | 有没有首次登录强制改密码的字段(如 `needUpdatePassword`),是否真的生效 | 先停下来和用户商量;备选:初始密码只走一次性渠道,并在核对报表里提示"从没改过初始密码" |
| S3 | `update-user` 加 `columns=properties` 能写入,不冲掉 `oauth_Lark_*` | 改用全字段回写,或换别的字段 |
| S4 | 停用后,密码登录和 password grant 都被拒 | 这是硬前提;不满足就不上线 |
| S5 | 关掉 `/signup` 页(signup items、邀请码等)后,接口层面能不能拦住自助注册;飞书扫码注册是否受影响,要等真实飞书联调时确认 | 只靠治理层闸门,部署文档里说明 |
| S6 | 管理员 API 建号会不会触发 webhook(此前实测:admin 操作的 record org 是 `built-in`) | 无论哪种结果,闸门都覆盖 |

实测结果记到 `docs/casdoor-findings.md` 新的一节。

## 7. 测试

- **单元测试**(先写测试,再写实现;Casdoor 用 `httpx.MockTransport` 假接口,飞书发送改成捕获,沿用现有测试写法):
  - 规则:到期日边界(当天有效、次日过期、按时区)、属性缺失或格式错误、提醒档位选择(只发最小一档、续期后重新开始)、续期天数计算与上限;
  - 闸门:webhook、`/request-access`、批准三处,飞书账号行为不变;
  - 卡片按钮:只认当前负责人,非负责人点击被拒;
  - 扫描:过期、数据异常、负责人停用都走 `offboard.apply`;`get-users` 失败时整步跳过;
  - 连带停用:只停该负责人名下的启用账号,失败不影响离职结果;
  - 对账把负责人纳入检查范围;重新启用后能重新申请(`expired` 状态);
  - 核对报表新异常;命令行参数校验、上限校验、退出码。
- **端到端实测**(新脚本,临时 Casdoor;飞书发送改成捕获):
  1. 建号 → 账号处于停用状态 → 负责人确认 → 能用邮箱登录;
  2. 申请应用 → 审批卡片带身份行 → 批准;
  3. 把到期日改成昨天 → 扫描后账号停用、准入清空;
  4. 续期 → 账号重新启用,准入要重新申请,而且申请能发出;
  5. 负责人离职 → 名下账号连带停用;
  6. 自助注册一个陌生账号 → 被闸门拦下,核对报表报出;
  7. 非负责人点卡片按钮被拒。
- **需要真实飞书**:新卡片在 PC 和手机上的显示、按钮回调,加进 README 的联调清单。

## 8. 交付顺序

分支 `feat/m4a-managed-accounts`,按以下顺序分步提交:

1. 实测 S1–S6,结果写进 findings。
2. `casdoor_admin` 辅助函数。
3. `accounts` 模型和规则。
4. **闸门**:单独就能补上 1.1 的风险,可以先合进 main。
5. 建号、纳管、负责人确认(命令行 + 确认卡片)。
6. 到期扫描、续期提醒、续期、重新申请。
7. 负责人离职连带停用 + 对账纳入负责人。
8. 换负责人、设密码、`list`。
9. 核对报表、审计导出、备份。
10. 文档:README、部署 README(Casdoor 关注册、密码策略、password grant)、接入说明、design 状态。

## 9. 不在本次范围

- 外部 OIDC/SAML 身份提供方(有具体对接方时再加,只需配置 Casdoor,并把外部登录绑到已建账号)。
- HR 系统对接。
- 负责人在飞书表单里自助发起建号。
- 外部账号的 MFA:建议后续给 `partner` 开 Casdoor TOTP,这一版只在部署文档里提示。
- 把 `evauth_kind` 放进 token。
- 里程碑 4b:账号自动供给/回收。
