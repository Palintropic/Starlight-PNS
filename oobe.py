# oobe.py — 首次运行配置向导
import os
import sys
import getpass

ENV_FILE = ".env"

PROVIDERS = {
    "1": {
        "name": "mimo (小秘喵代理)",
        "format": "anthropic",
        "base_url": "https://token-plan-cn.xiaomimimo.com",
        "key_name": "MIMO_API_KEY",
        "models": ["mimo-v2.5-pro", "claude-sonnet-4-5-20251001", "claude-haiku-4-5-20251001"],
    },
    "2": {
        "name": "Anthropic 直连",
        "format": "anthropic",
        "base_url": "https://api.anthropic.com",
        "key_name": "ANTHROPIC_API_KEY",
        "models": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
    },
    "3": {
        "name": "DeepSeek",
        "format": "openai",
        "base_url": "https://api.deepseek.com",
        "key_name": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "4": {
        "name": "Gemini",
        "format": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_name": "GEMINI_API_KEY",
        "models": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-2.5-flash"],
    },
}


def print_banner():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Project Nightcord Sanctuary        ║")
    print("  ║   首次运行配置 / First-time Setup     ║")
    print("  ╚══════════════════════════════════════╝")
    print()


def choose_provider() -> dict:
    print("  选择模型提供商：\n")
    for k, v in PROVIDERS.items():
        print(f"    {k}. {v['name']}")
    print()

    while True:
        choice = input("  输入编号 [1-4]：").strip()
        if choice in PROVIDERS:
            return PROVIDERS[choice]
        print("  请输入 1 到 4 之间的数字")


def choose_model(provider: dict) -> str:
    print(f"\n  可用模型（{provider['name']}）：\n")
    for i, m in enumerate(provider["models"], 1):
        print(f"    {i}. {m}")
    print(f"    {len(provider['models']) + 1}. 手动输入")
    print()

    while True:
        choice = input(f"  输入编号 [1-{len(provider['models']) + 1}]：").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(provider["models"]):
                return provider["models"][idx - 1]
            if idx == len(provider["models"]) + 1:
                return input("  输入模型名称：").strip()
        print("  无效输入，请重试")


def input_api_key(key_name: str) -> str:
    print(f"\n  输入 {key_name}（输入时不显示）：")
    while True:
        key = getpass.getpass("  > ").strip()
        if key:
            return key
        print("  API Key 不能为空")


def write_env(provider: dict, model: str, api_key: str):
    lines = [
        f"PROVIDER={provider['name']}",
        f"API_FORMAT={provider['format']}",
        f"BASE_URL={provider['base_url']}",
        f"MODEL={model}",
        f"{provider['key_name']}={api_key}",
        "PNS_API_KEY_NAME=" + provider["key_name"],
    ]

    # 保留 .env 里其他已有的条目（不覆盖无关变量）
    existing = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()

    managed_keys = {"PROVIDER", "API_FORMAT", "BASE_URL", "MODEL",
                    provider["key_name"], "PNS_API_KEY_NAME"}

    for k, v in existing.items():
        if k not in managed_keys:
            lines.append(f"{k}={v}")

    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    print_banner()

    provider = choose_provider()
    model = choose_model(provider)
    api_key = input_api_key(provider["key_name"])

    write_env(provider, model, api_key)

    print()
    print("  ✅ 配置已保存到 .env")
    print(f"     提供商：{provider['name']}")
    print(f"     模型：  {model}")
    print(f"     格式：  {provider['format']}")
    print()
    print("  现在可以运行：python run.py")
    print()


if __name__ == "__main__":
    main()
