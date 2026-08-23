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

### 3.1 每条断言由什么盯着

| 断言 | 自动化验证 |
|---|---|
| A1 A2 A3 A5 A6 A8 | `scripts/docker_smoke.sh`（真镜像 + 真容器）与 `tests/test_deployment_process.py`（真进程 + 真信号）|
| A4 A9 A10 A11 | `tests/test_deployment_security.py` |
| A5（日志与镜像层） | `tests/test_deployment_state.py`、`tests/test_deployment_package.py` |
| A7 | `tests/test_world_lifecycle.py`（P12 既有）与 `tests/test_deployment_process.py` |
| 交付包本身 | `tests/test_deployment_package.py` |

静态验证与一次真实的容器运行是两件事。交付报告要分开写明各跑了哪些，不允许把前者说成后者。

跑一遍真容器验收：

```bash
./scripts/docker_smoke.sh          # 跑完自动清理
KEEP=1 ./scripts/docker_smoke.sh   # 留下容器和卷供人工查看
```

它用独立的 Compose 项目名、独立的临时 env 文件和独立的卷，不碰仓库里的 `.env`，也不碰
默认部署；provider 指向一个不可路由的地址，所以整个过程不可能发生一次真实模型调用。

---

## 4. 前置条件

Ubuntu Server（22.04 / 24.04 LTS 均可），本地 ext4 根盘，虚拟机上装 Docker Engine 与 Compose 插件。
**不要**用 `docker.io` 那个发行版包，也不要装 Docker Desktop：

```bash
# Docker 官方仓库（Ubuntu）
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker "$USER"   # 重新登录后生效
docker compose version            # 确认插件在（不是 docker-compose 那个老脚本）
```

再确认三件跟耐久性直接相关的事：

```bash
findmnt -no FSTYPE /var/lib/docker   # 应当是 ext4（或别的本地文件系统），不能是 nfs/cifs
timedatectl                          # 时钟同步开着——世界时间与归档时间戳都来自宿主时钟
df -h /var/lib/docker                # 卷装在这里；存档会随世界一起长大
```

## 5. 目录与卷布局

```text
/opt/starlight-pns/          ← 克隆到哪里由你决定，这里用它举例
├── compose.yaml
├── Dockerfile
├── .env                     ← 你从 .env.example 复制出来的，chmod 600，不进版本库
└── ...仓库其余内容

Docker 具名卷（不在上面这棵树里）
├── starlight-pns_pns-data     → /app/data
│   ├── worlds/<world_id>/world.json    世界存档
│   ├── worlds/<world_id>/OWNER.lock    所有权锁
│   ├── drift_scores.jsonl              判分记录
│   └── review_decisions.jsonl          人工审核决策
└── starlight-pns_pns-history  → /app/history   会话归档 Markdown
```

镜像里的 `/app/data` 与 `/app/history` 是空目录且归 10001:10001 所有，具名卷首次挂载会继承这份
属主——所以用具名卷时宿主上**不需要**任何 `chown`。

想改用宿主绑定挂载（便于直接备份/查看）时，把 `compose.yaml` 的 volumes 换成：

```yaml
    volumes:
      - /srv/pns/data:/app/data
      - /srv/pns/history:/app/history
```

并且**必须**先把属主交给容器里的应用用户，否则非 root 的进程写不进去：

```bash
sudo mkdir -p /srv/pns/data /srv/pns/history
sudo chown -R 10001:10001 /srv/pns
```

绑定挂载的目标必须在本地文件系统上。NFS/SMB 会同时废掉 `flock`、原子替换和目录 `fsync` 这三条，
本板不声称支持那种拓扑。

## 6. 首次部署

```bash
sudo mkdir -p /opt/starlight-pns && sudo chown "$USER" /opt/starlight-pns
git clone https://github.com/Palintropic/Starlight-PNS.git /opt/starlight-pns
cd /opt/starlight-pns

cp .env.example .env
chmod 600 .env

# 生成管理凭据（至少 32 字符；示例占位串会被服务器拒绝）
openssl rand -hex 32
$EDITOR .env        # 填 PNS_ADMIN_TOKEN、provider 那四行、模型名

docker compose up -d --build
docker compose ps   # STATUS 应当从 starting 变成 healthy
```

第一次构建要拉两个基础镜像并装依赖，通常几分钟。

**默认只绑回环。** 想从别的机器访问，正确做法是在这台虚拟机上装反向代理（nginx / Caddy）
终结 TLS 再转发到 127.0.0.1:7860，并把 `.env` 里的 `PNS_SESSION_COOKIE_SECURE` 改成 `true`。
只有在你明确清楚这条端口上是什么的情况下，才把 `PNS_BIND` 改成 `0.0.0.0`。

## 7. 验证

一次成功的首次部署应当能逐条复现下面这些。它们直接对应第 3 节的验收条件。

```bash
BASE=http://127.0.0.1:7860
TOKEN=$(grep '^PNS_ADMIN_TOKEN=' .env | cut -d= -f2-)

# A1 就绪与同源。/readyz 公开，Dashboard 和 API 是同一个源。
curl -s $BASE/readyz            # {"status":"ready","mode":"production",...}
curl -s -o /dev/null -w '%{http_code}\n' $BASE/      # 200，Dashboard 首页

# A4 未授权请求执行不了管理操作
curl -s -o /dev/null -w '%{http_code}\n' $BASE/api/persistent-worlds          # 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/api/config/reload      # 401
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer wrong" $BASE/api/persistent-worlds                # 401

# 带凭据才做得了事
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/persistent-worlds

# A5 凭据不在镜像、不在前端包、不在日志里
docker history --no-trunc starlight-pns:local | grep -c "$TOKEN"              # 0
docker compose exec app grep -rl "$TOKEN" /app/dashboard/dist                 # 无输出
docker compose logs --no-color | grep -c "$TOKEN"                             # 0

# A2 活数据不在镜像层上：不挂卷跑一个新容器，data 是空的
docker run --rm --entrypoint sh starlight-pns:local -c 'ls -A /app/data'      # 无输出
```

浏览器打开 `http://127.0.0.1:7860`（或你的反代域名），会先看到登录框；贴入 `PNS_ADMIN_TOKEN`
之后进入后台。凭据换成一张 HttpOnly Cookie，此后不再经过浏览器。

> **恢复和 Start 都是手动的。** 容器起来之后不会自动打开任何世界，打开世界也不会自动开始
> 推进时间。要让一个世界活起来：在「持久世界」页按「恢复」，确认状态之后再按「开始自动推进」。
> 这是刻意的——重启一次就自己开始花模型额度是这套系统明确不做的事。

## 8. 日常操作

```bash
docker compose ps                  # 状态与健康
docker compose logs -f --tail 100  # 跟日志（日志按 20MB × 5 份轮转）
docker compose stop                # 正常停机，见下
docker compose start               # 再起来
docker compose restart             # 停 + 起
```

**正常停机会发生什么**（`docker compose stop` / `docker compose down`）：

1. Docker 发 SIGTERM，容器里的 python 是 PID 1，直接收到；
2. uvicorn 停止接受新连接，等已有连接收尾，上限 `PNS_GRACEFUL_TIMEOUT`（30 秒）；
3. 应用请所有自主驱动停下，有界等待（约 3 秒）；
4. 对本进程打开的每个世界走 P12 的安全关闭：停准入 → 等事务落定 → 最后一次 checkpoint →
   归还所有权；
5. 日志里给出逐个世界的结果。

停机预算合计约 35 秒，`stop_grace_period` 给的是 90 秒——余量是刻意的，因为第 4 步的耗时
取决于世界有多大。**改小它之前先想清楚**：宽限不够的后果是容器在最后一次 checkpoint 完成
之前被 SIGKILL，一次本可以干净的关闭变成一次崩溃恢复。

日志里应当看到：

```text
[persistent-worlds] 世界 'nightcord' 已干净关闭（第 42 版）
```

看到下面这句则表示**没有**关干净——那一版没存下去，能恢复到的仍然是上一次成功的 checkpoint：

```text
[persistent-worlds] 世界 'nightcord' **没有**干净关闭：…
```

这种情况下服务器不会把所有权标成"干净释放"。下一次恢复时状态里的 `recovered_from` 会带着
上一个拥有者的记录——那是实话，不要把它当成故障残留清掉。

> `docker compose ps -a` 里正常停机的退出码是 **143**（128 + SIGTERM）。这是 `docker stop`
> 期望看到的形状，不是失败。

## 9. 升级

卷不动，只换镜像。

```bash
cd /opt/starlight-pns
docker compose logs --no-color --tail 200 > /tmp/pns-before-upgrade.log   # 留一份现场
# 升级前先备份（见第 11 节），跨版本的存档兼容性没有承诺
git fetch origin
git checkout <目标 tag 或 commit>
docker compose up -d --build
docker compose ps            # 等 healthy
```

`up -d --build` 会重建镜像、替换容器，两个卷原封不动。容器换了之后世界仍然是关着的：
按第 7 节末尾那段手动恢复、确认，再决定要不要开始推进。

升级后至少确认三件事：`/readyz` 是 200、无凭据请求仍然是 401、世界恢复出来的 revision
不低于升级前记下的那个。

## 10. 回滚

```bash
cd /opt/starlight-pns
git checkout <上一个已知可用的 tag 或 commit>
docker compose up -d --build
```

**回滚前必须知道的一件事**：世界存档里**没有 schema 版本号**。所以一份由新版本写出来的存档，
不保证旧版本读得回来——旧版本可能报 `archive_corrupt` / `archive_unusable`，也可能读出来但
少了新字段。因此：

- 升级之前一定要按第 11 节备份卷；
- 回滚时如果新版本已经写过存档，正确顺序是**先回滚代码，再把备份恢复回去**，而不是让旧代码
  去读新存档；
- 只跑过读操作、没写过存档的升级可以直接回滚。

## 11. 备份与恢复

具名卷的备份用一个临时容器打包，宿主上不需要 root 去翻 `/var/lib/docker`：

```bash
cd /opt/starlight-pns
docker compose stop                     # 停机备份，拿到的是一致快照

STAMP=$(date +%Y%m%d_%H%M%S)
docker run --rm \
  -v starlight-pns_pns-data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine tar czf "/backup/pns-data-$STAMP.tar.gz" -C /data .
docker run --rm \
  -v starlight-pns_pns-history:/history:ro \
  -v "$PWD/backups:/backup" \
  alpine tar czf "/backup/pns-history-$STAMP.tar.gz" -C /history .

docker compose start
```

热备份（不停机）拿到的是一个**跨越写入边界**的快照，可能落在一次 checkpoint 中间。真要做，
先在后台对每个开着的世界按一次 checkpoint，再立刻备份，并且明白这份备份只到那一刻为止。

恢复：

```bash
docker compose down                     # 容器必须先停，否则会跟活着的写者抢同一份数据
docker run --rm \
  -v starlight-pns_pns-data:/data \
  -v "$PWD/backups:/backup" \
  alpine sh -c 'rm -rf /data/* /data/..?* 2>/dev/null; tar xzf /backup/pns-data-<STAMP>.tar.gz -C /data'
docker compose up -d
```

恢复完之后进后台恢复世界，确认 revision 和世界时钟是你期望的那一份，再决定要不要 Start。

`.env` 不在卷里，它在仓库目录下。它是唯一一份**没有**被上面这套备份覆盖的东西——单独备份它，
并且不要备份到任何会被别人读到的地方。

## 12. 异常停机之后

虚拟机断电、`docker kill`、OOM killer——这几种都属于强杀。此时的承诺**只有一条**：能恢复到的
是最后一次成功的 checkpoint，一次不多、一次不少。checkpoint 之后提交的东西没了。

强杀之后要做的事：

```bash
docker compose up -d
docker compose logs --tail 50
```

然后在后台恢复世界，看两个字段：

- `recovered_from` 非空 → 上一个拥有者是崩掉的，不是干净走的。这是实话，正常现象；
- `residue` 非空 → 磁盘上留了写到一半的临时文件。它们不影响读回最后一份完整存档；
  确认服务正常之后可以在卷里删掉。

进程锁（`OWNER.lock`）是 `flock`，随进程死亡由内核释放，所以强杀**不会**留下一把需要人工
清理的死锁。如果恢复时报 `world_already_open`，那说明真的还有另一个写者活着——先去找它，
不要去删锁文件。

## 13. 启动失败

生产模式缺必填项时进程会直接起不来，这是**设计如此**：一台配置坏掉的服务器应该起不来，
而不是带着一个开着的管理面跑起来。

```bash
docker compose ps          # STATUS 一直在 Restarting
docker compose logs --tail 30
```

日志末尾会写明缺什么。常见几种：

| 日志里的话 | 原因 | 修法 |
|---|---|---|
| `PNS_ADMIN_TOKEN 至少要 32 个字符` | 凭据太短 | `openssl rand -hex 32` 重新生成 |
| `PNS_ADMIN_TOKEN 还是示例占位串` | `.env` 是照抄的 | 换成真随机值 |
| `生产模式必须提供 PNS_ADMIN_TOKEN` | 没设 | 见上 |
| `生产模式必须注入模型凭据 <VAR>` | provider key 变量名和值对不上 | 核对 `PNS_API_KEY_NAME` 与那一行 key |
| `生产模式要求已构建的 Dashboard` | 镜像构建阶段出了问题 | `docker compose build --no-cache` |
| `环境变量 … 不是合法数值` | 某个可选项写错了 | 按提示改，服务器不会悄悄回落到默认值 |

`restart: unless-stopped` 会让它一直重试，Docker 按指数退避（不是热循环）。修好 `.env`
之后 `docker compose up -d` 就能起来；确认恢复之前想让它停下就 `docker compose stop`——
`unless-stopped` 的意思正是"人工停的就别自己起来"。

## 14. 这套部署里做不了的事

写下来是为了不让人在生产上试：

- **World Editor 保存和 Setup Wizard 保存在生产模式下返回 409。** 它们写的是镜像层里的
  `pns/world/*.py` 和 `.env`——前者下次 `up --build` 就没了，后者还会盖住 Compose 注入的
  配置。改内容和改 provider 的正确做法是改仓库/改 `.env` 再重新部署。读接口不受影响。
- **一份数据同时只能有一个应用容器。** 固定的 `container_name` 让第二次 `up` 撞名字失败；
  就算绕过去，世界层面的所有权锁也会让第二个写者拿到 `409 world_already_open`。
- **不支持 NFS/SMB 上的数据目录**，理由见第 1 节。
- **没有零停机部署、没有横向扩展、没有集中式日志或自动异地备份**，这些都不在本板范围内。
- **Sekai Times / WordPress 集成不在这里**，那是 `ST-1`。
