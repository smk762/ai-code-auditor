from __future__ import annotations


def propose_library_name(domain: str) -> str:
    normalized = domain.strip().lower().replace(" ", "-")
    if not normalized:
        normalized = "shared-utils"
    if not normalized.startswith("shared-"):
        normalized = f"shared-{normalized}"
    return normalized


def build_public_api(symbols: list[str]) -> list[str]:
    if not symbols:
        return ["run(input: dict) -> dict"]
    return [f"{symbol}(input: dict) -> dict" for symbol in symbols[:4]]


def phased_migration_plan(package_name: str, repos: list[str]) -> dict:
    early = repos[:1] if repos else ["pilot-repo"]
    later = repos[1:] if len(repos) > 1 else repos
    return {
        "phase_1": {
            "name": "Create compatibility wrapper",
            "tasks": [
                f"Create {package_name} package with stable API.",
                "Add adapters preserving old function signatures.",
            ],
        },
        "phase_2": {
            "name": "Adopt in low-risk services first",
            "tasks": [f"Migrate imports in {repo} to {package_name}." for repo in early] or ["Migrate one low-risk service."],
        },
        "phase_3": {
            "name": "Ecosystem rollout and cleanup",
            "tasks": (
                [f"Migrate {repo} to {package_name}." for repo in later]
                + ["Remove duplicate helpers.", f"Enforce architecture rule requiring {package_name}."]
            ),
        },
    }
