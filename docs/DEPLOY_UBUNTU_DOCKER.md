# Ubuntu Server + Docker 部署边界（DEPLOY-1）

本文是 Starlight-PNS 在**独立 Ubuntu Server 虚拟机 + Docker Engine + Compose** 上的部署契约。
第一节到第三节是边界与验收条件（写在实现之前，用来判定实现有没有成立）；第四节起是操作手册。

运维边界：ESXi、虚拟机、网络、DNS、TLS 和真实凭据属于操作者。本仓库只交付文件和步骤，
不登录、不改动任何服务器。

---

## 1. 固定拓扑

```text
ESXi
└─ Ubuntu Server VM（本地 ext4）
   └─ Docker Engine + Docker Compose
      └─ starlight-pns 容器（FastAPI + 已构建的 Dashboard，同一个源）
         ├─ 具名卷 pns-data     → /app/data     （世界存档、所有权锁、评分与审核记录）
         └─ 具名卷 pns-history  → /app/history  （会话归档 Markdown）
```

不引入、也不支持：WSL、Windows 容器、Docker Desktop、Kubernetes、多机编排、
共享网络文件系统（NFS/SMB）、ESXi 自动化层。Sekai Times / WordPress 集成属于 `ST-1`，不在本板。

P12 的耐久性建立在**本地文件系统**语义上：`flock` 排他锁、`os.replace` 原子替换、目录 `fsync`。
把 `/app/data` 放到 NFS/SMB 上会让这三条同时失去意义，本板不声称支持那种拓扑。

---

## 2. 安全与生命周期边界

| # | 边界 | 机制 |
|---|---|---|
| B1 | 构建期没有任何密钥 | `.dockerignore` 排除 `.env` 与所有运行时数据；镜像里没有 `.env`；凭据只在运行时由 Compose 注入 |
| B2 | 管理操作必须鉴权 | 默认拒绝：除显式公开清单外的每一条路径（含 `/ws/run`）都要求已认证主体 |
| B3 | 鉴权在服务端且 fail-closed | 生产模式下缺少/不合法的 `PNS_ADMIN_TOKEN` 让**进程起不来**，不回落到开发模式；浏览器拿到的是服务端签发的 HttpOnly 会话 Cookie，密钥不进 JS 包、不进 URL |
| B4 | 运行时数据在镜像之外 | 存档、锁文件、评分与审核记录、会话归档全部落在具名卷上；重建容器、换镜像都不碰它们 |
| B5 | 文件系统语义诚实 | 单写者：一个数据目录同时只允许一个应用容器；重复启动必须响亮失败，不是静默共写 |
| B6 | 重启不等于 Start | 进程启动、容器重建、恢复世界都不会打开自主驱动；**只有**已认证操作者显式 Start 才开始花模型额度 |
| B7 | 停机有界且不说谎 | SIGTERM → 停止接新工作 → 等生命周期边界 → 最后一次 checkpoint → 如实报告；存不下去的世界不会被说成"干净关闭"，也不会被 release。Compose 的 `stop_grace_period` 大于应用停机预算；SIGKILL 之后能恢复到的只有最后一次成功 checkpoint |
| B8 | 健康检查不是耐久性表演 | `/healthz` 只回答"进程还能应答"，`/readyz` 只回答"启动配置完成了"；两者都不调用模型、不推进时间、不重载配置、不获取世界所有权、不泄露任何密钥或状态 |
| B9 | 一个生产源 | Dashboard 与 API 同源，由同一个 uvicorn 进程提供；生产不用 Vite dev server，不用自动重载的开发服务器，不靠 CORS 拼凑拓扑 |

### 2.1 生产模式与开发模式

两种模式**显式分开**，由 `PNS_ENV` 决定，生产镜像在 Dockerfile 里把它固化成 `production`：

- `PNS_ENV=production`：`PNS_ADMIN_TOKEN`（≥32 字符）、模型 provider 凭据、已构建的 Dashboard
  三者缺一不可，缺任何一样**启动失败**；写回仓库源码的接口（World Editor、`POST /api/config`）
  一律拒绝，因为镜像层的写入在下一次重建时就没了。
- 其它取值（默认 `development`）：保持既有本地开发行为不变——没有 `PNS_ADMIN_TOKEN` 就不鉴权，
  启动时打印一条明确的警告。这条路径永远不会被生产镜像走到。

---

## 3. 可证伪验收条件

每一条都写明**怎么让它失败**。绿灯本身不算证据，能被证伪的绿灯才算。

| # | 断言 | 证伪方法 |
|---|---|---|
| A1 | 一次干净构建能通过 Compose 起来，Dashboard 与 API 同源可用，`/readyz` 只在配置可用之后才 healthy | 拿掉必填配置起容器：如果它仍然 healthy，或 Dashboard 打得开却调不通 API，A1 就是假的 |
| A2 | 重建容器不丢世界 | 建世界、提交可辨认状态、`docker compose down` 后重建镜像与容器、恢复；如果 revision 或状态回退，或状态其实来自镜像层（`docker diff` / 镜像内搜索能找到活数据），A2 就是假的 |
| A3 | 重启不会自动开始模型调用 | 把 provider 指向一个会计数的本地假端点；重启进程/容器、恢复世界、等待若干个 tick 周期，计数必须是 0；同一个假端点在显式 Start 之后必须计到 ≥1（否则计数器本身没有证伪能力） |
| A4 | 每个特权端点在做任何变更之前拒绝缺失/畸形/错误的凭据 | 无头、错 scheme、错值、重复头、错 Cookie 各打一次；拒绝之后世界目录的文件指纹、revision、活动状态必须逐字节不变 |
| A5 | 运行时密钥不出现在镜像、前端包、健康/状态响应或日志里 | 在 `docker history`、镜像文件全文搜索、`dashboard/dist` 全文搜索、所有公开与私有响应正文、以及捕获的 stdout/stderr（**含异常路径**）里搜索金丝雀密钥，命中任意一处 A5 就是假的 |
| A6 | SIGTERM 走文档写明的生命周期，容器不在边界落定之前报告已停止 | 空闲时与一次有界操作进行中各发一次 SIGTERM：进程必须在 grace 内退出、打印如实的关闭报告、磁盘上是最后一次成功 checkpoint；强杀之后恢复出来的必须正好是最后一次成功 checkpoint，不多不少 |
| A7 | checkpoint 失败不会被说成干净关闭，也不会丢掉唯一的恢复证据 | 让最后一次 checkpoint 失败：如果进程仍打印"干净关闭"、或锁记录被写成 `released`（等于告诉下一个拥有者上一个是干净走的），A7 就是假的 |
| A8 | 两个应用容器不能静默写同一个世界目录 | 对同一个数据目录起第二个写者：必须在 Compose 层（容器名冲突）或应用层（`409 world_already_open`）响亮失败，不允许两个进程同时持有同一个世界 |
| A9 | 生产缺少必填安全/配置项时启动失败，测试不能靠宽松默认值拿绿灯 | 逐项删掉 `PNS_ADMIN_TOKEN`、模型凭据、Dashboard 产物，各起一次：任何一次起得来，A9 就是假的。同时：把鉴权中间件摘掉之后，A4 的用例必须变红 |
| A10 | 公开面是一份显式清单，不是"忘了保护"的默认继承 | 新增一条路由而不改清单：它必须默认被保护。`dashboard/dist` 里出现清单没覆盖的顶层文件时，测试必须失败 |
| A11 | 健康检查没有权威副作用 | 连续打 `/healthz`、`/readyz`：世界存档根不许被创建，配置 revision 不许前进，模型假端点计数必须是 0，不许有世界被打开 |
| A12 | 既有验证全绿 | 全量 Python 测试、Dashboard 测试与构建、`compileall`、`git diff --check` |

### 3.1 环境能力声明

本仓库的实现环境（macOS + 无 Docker daemon 时）能跑的是**静态与进程级**验证：
A4/A5/A6/A7/A9/A10/A11/A12 全部由自动化测试覆盖，A1/A2/A3/A8 的进程级等价物由测试覆盖，
其容器级形态由 `scripts/docker_smoke.sh` 在有 Docker 时验证。
交付报告必须写明这两类验证分别跑了哪些、没跑哪些——不允许把静态验证说成一次真实的容器运行。
