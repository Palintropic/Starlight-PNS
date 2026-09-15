# Sekai Times 待审核投稿出站层

`pns/integrations/sekai_times.py` 是 PNS → WordPress 的 ST-1 出站边界。
它只投递 `pending` 文章，不替角色决定写稿，也不自动从普通对白生成文章。
目前运行时还没有已提交的角色发文动作，因此这层暂未挂到自主运行循环。

调用方须拿到一个角色来源的稳定 ID、世界和 session 身份、标题、正文，以及
与来源 ID、角色、标题和正文逐字绑定且已接受的 `GenerationAudit`，然后调用
`PendingPostOutbox.enqueue(post, audit)`。队列应放在 PNS 的持久 `data/` 卷，
例如 `data/sekai_times_posts.sqlite3`。客户端可用
`WordPressPendingPosts.from_env()` 从环境变量
`PNS_SEKAI_TIMES_URL`、`PNS_SEKAI_TIMES_USERNAME` 和
`PNS_SEKAI_TIMES_APPLICATION_PASSWORD` 构造客户端；生产 URL 必须是 HTTPS。

每份投稿按 world ID 与来源 ID 计算 `pns_submission_id`。WordPress 的
`pending` 文章实测可能返回空 slug，所以失败对账查询已认证的
`context=edit` 文章元数据，不凭 slug 重发。写入前队列先把投稿标为
`uncertain`；如果响应丢失而远端查不到标记，它保持待对账状态，不自动再次
POST。4xx 返回也先对账：若能找到文章则进入 `sent`，查不到时进入
`blocked` 并留待人工检查；成功或找到既有文章进入 `sent`。

WordPress 主题必须注册 edit-context `pns_submission_id` 字段，且低权限
`pns_bot` 只能提交待审核文章。PNS 和 Sekai Times 两仓库的 ST-1 分支须一起
部署；只部署 PNS 会因远端不认识新标记而留下无法自动对账的投稿。

2026-09-16 本地开发副本实测：文章 7 创建为 `pending`，重复 dispatch 返回
同一 ID；模拟丢弃创建响应后，文章 8 被远端标记找回，没有第二次 POST。
文章 5、8 是测试夹具，已移到本地回收站；文章 7 仍在本地待审核队列，
标题明确注明不可发布。测试夹具不属于 PNS 世界历史，也不证明角色自主发文
路径已存在。日本生产站点没有收到测试投稿。联调用的本地 Application Password
已撤销，临时凭据与测试 outbox 已清理；后续正式联调需通过运行环境重新注入
低权限凭据。
