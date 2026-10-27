from __future__ import annotations

from opencivicquery.models import DatasetProfile, DeploymentBundle, PackSummary


class PackRegistry:
    """Looks up curated dataset profiles by portal domain and dataset ID."""

    def __init__(self, bundle: DeploymentBundle) -> None:
        self._bundle = bundle
        self._profiles = {
            (pack.domain, profile.dataset_id): profile
            for pack in bundle.packs
            for profile in pack.datasets
        }

    @property
    def deployment_id(self) -> str:
        return self._bundle.config.deployment_id

    @property
    def config_sha256(self) -> str:
        return self._bundle.config_sha256

    def profile_for(self, domain: str, dataset_id: str) -> DatasetProfile | None:
        return self._profiles.get((domain, dataset_id))

    def summaries(self) -> list[PackSummary]:
        return [
            PackSummary(
                pack_id=pack.pack_id,
                name=pack.name,
                domain=pack.domain,
                checked_on=pack.checked_on,
                datasets=[
                    {"dataset_id": profile.dataset_id, "name": profile.name}
                    for profile in pack.datasets
                ],
            )
            for pack in self._bundle.packs
        ]
