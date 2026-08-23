# tests/test_deployment_package.py — DEPLOY-1 交付物本身的不变量。
#
# 这一组测的不是运行时行为，是**交付包**：Dockerfile、compose.yaml、
# .dockerignore、.env.example、requirements-prod.txt、健康探针。它们错了不会
# 让任何单元测试变红，只会让第一次真实部署出事，所以它们需要自己的守卫。
#
# 盯住的东西按"错了会怎样"排：
#   1. 构建上下文默认排除。新出现的文件默认进不了镜像；`.env` 和运行时数据
#      在任何情况下都进不去。
#   2. 本机 `.env` 里的真凭据不出现在任何一份会进构建上下文的文件里。
#   3. 镜像不以 root 跑，基础镜像按 digest 钉死，装的是生产依赖子集。
#   4. Compose 是单写者拓扑：一个副本、固定容器名、显式健康检查、显式重启
#      策略、运行时数据在卷上、停机宽限大于应用停机预算。
#   5. .env.example 里没有真值，而且它的管理 token 占位串必须是代码认得出来
#      的那个占位串——否则"照抄示例配置起不来生产"这句话是假的。
#   6. 健康探针不吞失败：连不上、非 200，一律不健康。
#
# 运行: python -m unittest tests.test_deployment_package -v
import fnmatch
import http.server
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import yaml  # noqa: E402

import healthcheck  # noqa: E402
from pns.interfaces.security import (  # noqa: E402
    MIN_ADMIN_TOKEN_CHARS,
    PLACEHOLDER_TOKENS,
)

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "compose.yaml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_PROD = REPO_ROOT / "requirements-prod.txt"
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY_UBUNTU_DOCKER.md"

# .dockerignore 放行的顶层条目。改这份清单就是在改"什么进得了镜像"，
# 所以它在测试里也写一遍：两边对不上时必须有人当场做决定。
ALLOWED_CONTEXT_ENTRIES = {
    "pns",
    "scripts",
    "packs",
    "dashboard",
    "config.yaml",
    "requirements-prod.txt",
}


# `*_NAME` 装的是变量名而不是值（PNS_API_KEY_NAME=MIMO_API_KEY），它不是凭据。
CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def is_credential_name(name: str) -> bool:
    upper = name.strip().upper()
    if upper.endswith("_NAME"):
        return False
    return any(word in upper for word in CREDENTIAL_WORDS)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def dockerignore_rules():
    for line in read(DOCKERIGNORE).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        yield line


def included_in_context(name: str) -> bool:
    """按 Docker 的规则判断一个**顶层**条目会不会进构建上下文。

    规则很简单也很致命：从上往下匹配，最后一条匹配的赢，`!` 是放行。这里只
    判顶层——目录一旦被放行，里面的内容默认跟着进去，那正是 `.dockerignore`
    最常被误解的地方。
    """
    included = True
    for rule in dockerignore_rules():
        negated = rule.startswith("!")
        pattern = (rule[1:] if negated else rule).rstrip("/")
        if not pattern or "/" in pattern:
            # 带路径分隔符的规则针对的是目录**里面**的东西（例如
            # dashboard/node_modules），它决定不了顶层条目进不进上下文。
            continue
        if fnmatch.fnmatch(name, pattern):
            included = negated
    return included


class DockerignoreTests(unittest.TestCase):
    def test_context_is_deny_by_default(self):
        first = next(dockerignore_rules())
        self.assertEqual(first, "*", ".dockerignore 必须以全排除开头")

    def test_only_the_allowlisted_top_level_entries_enter_the_context(self):
        present = {entry.name for entry in REPO_ROOT.iterdir()}
        actual = {name for name in present if included_in_context(name)}
        self.assertEqual(
            actual,
            ALLOWED_CONTEXT_ENTRIES & present,
            "构建上下文的内容变了：要么改 .dockerignore，要么改这份清单——"
            "但必须有人当场决定，不能靠默认继承",
        )

    def test_secrets_and_runtime_data_never_enter_the_context(self):
        for name in (
            ".env",
            ".env.local",
            ".git",
            "data",
            "history",
            "tests",
            "kickoff",
            "review_decisions.jsonl",
            "secrets.yaml",
            ".venv",
        ):
            with self.subTest(name=name):
                self.assertFalse(included_in_context(name), f"{name} 会进构建上下文")

    def test_a_brand_new_top_level_file_is_excluded_by_default(self):
        """默认排除的意义就在这一条：明天新出现的文件不会自己溜进镜像。"""
        self.assertFalse(included_in_context("some-new-thing.yaml"))
        self.assertFalse(included_in_context("credentials.json"))


class NoLocalSecretInBuildContextTests(unittest.TestCase):
    """本机 `.env` 里的真凭据不许出现在任何会进镜像的文件里。

    这条不是理论演练：开发机上的 `.env` 装着真的 API Key，而"把它抄进某份
    配置里"是这类事故最常见的形状。
    """

    SKIP_DIRS = {"node_modules", "dist", "__pycache__", ".git"}

    def local_secrets(self):
        """本机 `.env` 里**凭据**变量的值。

        只看名字里带 KEY/TOKEN/SECRET/PASSWORD 的那些，而且排除 `*_NAME`
        （那装的是变量名，不是值）。不这么收窄的话，BASE_URL 这种本来就写在
        代码默认值里的东西会让这条用例长期红着，红久了就没人看了。
        """
        env_file = REPO_ROOT / ".env"
        if not env_file.exists():
            return []
        values = []
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if not is_credential_name(name.strip()):
                continue
            value = value.strip().strip('"').strip("'")
            if len(value) >= 12:
                values.append(value)
        return values

    def context_files(self):
        for name in sorted(ALLOWED_CONTEXT_ENTRIES):
            path = REPO_ROOT / name
            if path.is_file():
                yield path
            elif path.is_dir():
                for child in path.rglob("*"):
                    if any(part in self.SKIP_DIRS for part in child.parts):
                        continue
                    if child.is_file():
                        yield child
        for extra in (DOCKERFILE, COMPOSE, ENV_EXAMPLE, DOCKERIGNORE):
            yield extra

    def test_no_local_env_value_appears_in_the_build_context(self):
        secrets = self.local_secrets()
        if not secrets:
            self.skipTest("本机没有 .env，没什么可泄露的")
        for path in self.context_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for secret in secrets:
                self.assertNotIn(secret, text, f"{path} 里出现了本机 .env 的值")


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.text = read(DOCKERFILE)
        self.lines = [line.strip() for line in self.text.splitlines()]

    def test_base_images_are_pinned_by_digest(self):
        froms = [line for line in self.lines if line.startswith("FROM ")]
        self.assertGreaterEqual(len(froms), 2, "应当是多阶段构建")
        for line in froms:
            image = line.split()[1]
            if image.upper() in ("SCRATCH",):
                continue
            with self.subTest(image=image):
                self.assertRegex(
                    image,
                    r"@sha256:[0-9a-f]{64}$",
                    "基础镜像必须按 digest 钉死——只钉 tag 的话同一份 "
                    "Dockerfile 两周后会构建出另一个镜像，而回滚要靠的正是可重现",
                )

    def test_runs_as_a_non_root_user(self):
        users = [line for line in self.lines if line.startswith("USER ")]
        self.assertTrue(users, "必须显式切到非 root 用户")
        self.assertNotIn("root", users[-1])
        self.assertIn("10001", users[-1])

    def test_user_switch_happens_after_the_copies(self):
        user_index = max(i for i, l in enumerate(self.lines) if l.startswith("USER "))
        copy_indexes = [i for i, l in enumerate(self.lines) if l.startswith("COPY ")]
        self.assertTrue(copy_indexes)
        self.assertGreater(user_index, max(copy_indexes))

    def test_installs_the_production_requirement_subset(self):
        self.assertIn("requirements-prod.txt", self.text)
        self.assertNotIn("-r requirements.txt", self.text)

    def test_does_not_copy_the_whole_context(self):
        for line in self.lines:
            if line.startswith("COPY ") and "--from=" not in line:
                source = line.split()[-2] if len(line.split()) >= 3 else ""
                with self.subTest(line=line):
                    self.assertNotIn(source, (".", "./"), "不许整包拷进镜像")

    def test_does_not_copy_tests_or_docs(self):
        for forbidden in ("tests/", "docs/", "kickoff/", ".git", "requirements.txt "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(f"COPY {forbidden}", self.text)

    def test_declares_no_secret_at_build_time(self):
        for line in self.lines:
            if line.startswith(("ARG ", "ENV ")):
                with self.subTest(line=line):
                    upper = line.upper()
                    for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"):
                        self.assertNotIn(word, upper, "构建期不许出现凭据")

    def test_production_mode_is_baked_into_the_image(self):
        """就算有人 `docker run` 忘了传 PNS_ENV，它也不该退回不鉴权的开发路径。"""
        self.assertRegex(self.text, r"ENV\s+PNS_ENV=production")

    def test_healthcheck_and_cmd_use_exec_form_and_the_shared_probe(self):
        self.assertIn('CMD ["python", "scripts/healthcheck.py"]', self.text)
        self.assertIn('CMD ["python", "scripts/server.py"]', self.text)

    def test_no_dev_server_in_the_image(self):
        for forbidden in ("npm run dev", "vite preview", "--reload"):
            self.assertNotIn(forbidden, self.text)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(read(COMPOSE))
        self.services = self.doc["services"]
        self.app = self.services["app"]

    def seconds(self, value):
        text = str(value).strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|h)?", text)
        self.assertIsNotNone(match, f"读不懂的时长：{value!r}")
        amount = float(match.group(1))
        return amount * {"s": 1, "m": 60, "h": 3600}[match.group(2) or "s"]

    def test_exactly_one_writer_service(self):
        self.assertEqual(list(self.services), ["app"])
        self.assertNotIn("replicas", self.app)
        self.assertNotIn("scale", self.app)
        deploy = self.app.get("deploy") or {}
        self.assertNotIn("replicas", deploy)

    def test_a_fixed_container_name_makes_a_second_writer_fail_loudly(self):
        """两个应用容器写同一个卷是本板明确不支持的拓扑。固定容器名让第二次
        `up` 撞名字失败，而不是静静共写。"""
        self.assertTrue(self.app.get("container_name"))

    def test_runtime_data_lives_on_declared_named_volumes(self):
        mounts = {m.split(":")[1]: m.split(":")[0] for m in self.app["volumes"]}
        self.assertIn("/app/data", mounts)
        self.assertIn("/app/history", mounts)
        declared = set(self.doc.get("volumes") or {})
        for name in mounts.values():
            self.assertIn(name, declared, f"{name} 没有在顶层 volumes 里声明")

    def test_root_filesystem_is_read_only_with_a_writable_scratch(self):
        self.assertTrue(self.app.get("read_only"))
        self.assertTrue(self.app.get("tmpfs"))

    def test_explicit_healthcheck_uses_the_shared_probe(self):
        check = self.app["healthcheck"]
        self.assertEqual(check["test"], ["CMD", "python", "scripts/healthcheck.py"])
        for key in ("interval", "timeout", "retries", "start_period"):
            self.assertIn(key, check)

    def test_explicit_restart_policy(self):
        self.assertEqual(self.app["restart"], "unless-stopped")

    def test_stop_grace_period_exceeds_the_application_shutdown_budget(self):
        """放不下的后果很具体：容器在最后一次 checkpoint 完成之前被 SIGKILL。"""
        grace = self.seconds(self.app["stop_grace_period"])
        graceful = float(self.app["environment"]["PNS_GRACEFUL_TIMEOUT"])
        self.assertGreater(grace, graceful * 2, "停机宽限没有给收尾留出余量")

    def test_runtime_configuration_is_injected_not_baked(self):
        self.assertIn(".env", self.app["env_file"])
        self.assertEqual(self.app["environment"]["PNS_ENV"], "production")

    def test_no_secret_literal_in_compose(self):
        text = read(COMPOSE)
        for word in ("PNS_ADMIN_TOKEN", "API_KEY"):
            self.assertNotIn(f"{word}:", text, "凭据不许写在 compose 里")

    def test_builds_from_this_repository(self):
        self.assertEqual(self.app["build"]["context"], ".")
        self.assertEqual(self.app["build"]["dockerfile"], "Dockerfile")


class EnvExampleTests(unittest.TestCase):
    def entries(self):
        for line in read(ENV_EXAMPLE).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            yield name.strip(), value.strip()

    def test_admin_token_placeholder_is_one_the_code_refuses(self):
        """照抄这份示例起不来生产——这条把文档里的那句话变成机制。"""
        values = dict(self.entries())
        placeholder = values["PNS_ADMIN_TOKEN"]
        self.assertIn(placeholder.lower(), PLACEHOLDER_TOKENS)
        self.assertGreaterEqual(
            len(placeholder),
            MIN_ADMIN_TOKEN_CHARS,
            "占位串要足够长，才能证明拦住它的是占位串检查而不是长度检查",
        )

    def test_no_value_looks_like_a_real_credential(self):
        for name, value in self.entries():
            if not is_credential_name(name):
                continue
            with self.subTest(name=name):
                self.assertTrue(
                    value.startswith("replace-with-"),
                    f"{name} 的示例值必须是占位符",
                )

    def test_documents_the_names_the_code_actually_reads(self):
        names = {name for name, _ in self.entries()}
        for required in ("PNS_ADMIN_TOKEN", "PNS_API_KEY_NAME", "API_FORMAT", "BASE_URL"):
            self.assertIn(required, names)

    def test_does_not_offer_a_way_to_turn_production_off(self):
        self.assertNotIn("PNS_ENV=", read(ENV_EXAMPLE))


class RequirementsTests(unittest.TestCase):
    def packages(self, path):
        found = set()
        for line in read(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            found.add(line.split("[")[0].split("==")[0].split(">=")[0].strip())
        return found

    def test_production_set_is_a_subset_of_the_development_set(self):
        self.assertTrue(
            self.packages(REQUIREMENTS_PROD) <= self.packages(REQUIREMENTS),
            "生产依赖里出现了 requirements.txt 没有的包",
        )

    def test_development_only_packages_are_excluded(self):
        excluded = self.packages(REQUIREMENTS) - self.packages(REQUIREMENTS_PROD)
        self.assertEqual(excluded, {"black", "httpx", "aiohttp"})

    def test_the_runtime_imports_are_all_covered(self):
        prod = self.packages(REQUIREMENTS_PROD)
        for package in ("fastapi", "uvicorn", "pydantic", "pyyaml", "python-dotenv"):
            self.assertIn(package, prod)

    def test_no_package_named_in_the_excluded_set_is_imported_by_shipped_code(self):
        """把 aiohttp 从镜像里拿掉的前提是没人 import 它。"""
        result = subprocess.run(
            ["grep", "-rn", "-E", r"^\s*(import|from)\s+aiohttp", "pns", "scripts"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "", "有代码 import 了 aiohttp")


class HealthProbeTests(unittest.TestCase):
    def serve(self, status):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/readyz"

    def test_healthy_on_200(self):
        self.assertEqual(healthcheck.probe(self.serve(200)), 0)

    def test_unhealthy_on_error_status(self):
        self.assertEqual(healthcheck.probe(self.serve(503)), 1)

    def test_unhealthy_when_nothing_is_listening(self):
        self.assertEqual(
            healthcheck.probe("http://127.0.0.1:1/readyz", timeout=1.0), 1
        )

    def test_probes_the_public_readiness_path_on_the_configured_port(self):
        url = healthcheck.url_from_env({"PORT": "9999"})
        self.assertEqual(url, "http://127.0.0.1:9999/readyz")
        self.assertTrue(healthcheck.url_from_env({}).endswith("/readyz"))


class DeployDocTests(unittest.TestCase):
    def test_the_operator_document_exists(self):
        self.assertTrue(DEPLOY_DOC.exists())


if __name__ == "__main__":
    unittest.main()
