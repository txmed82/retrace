# STAGE: plan-b — Linear, GitHub, and notification sink configuration.
from __future__ import annotations

from pydantic import BaseModel, Field


class LinearConfig(BaseModel):
    api_key: str = ""
    team_id: str = ""
    team_key: str = ""
    labels: list[str] = Field(default_factory=list)
    endpoint: str = "https://api.linear.app/graphql"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


class GitHubSinkConfig(BaseModel):
    api_key: str = ""
    repo: str = ""
    labels: list[str] = Field(default_factory=list)
    base_url: str = "https://api.github.com"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


class GitHubAppConfig(BaseModel):
    app_id: str = ""
    webhook_secret: str = ""
    private_key: str = ""
    installation_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.app_id.strip() and self.webhook_secret.strip())


class NotificationConfig(BaseModel):
    webhook_url: str = ""
    webhook_secret: str = ""
    slack_webhook_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip() or self.slack_webhook_url.strip())
