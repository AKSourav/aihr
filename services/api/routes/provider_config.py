from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import AuditEvent, Job, Organization, ProviderCredential
from core.schemas import ProviderSelection
from core.secrets import encrypt_secret, last4
from services.api.account_authz import AccountContext, owner_only

router = APIRouter(prefix="/accounts/{account_id}", tags=["providers"])

ProviderName = Literal["openai", "deepgram", "cartesia", "elevenlabs"]


class CredentialIn(BaseModel):
    provider: ProviderName
    api_key: str | None = None
    base_url: str | None = None


class CredentialOut(BaseModel):
    provider: str
    base_url: str | None
    key_last4: str
    updated_at: datetime


class ProviderSettingsRequest(BaseModel):
    selection: ProviderSelection = ProviderSelection()
    credentials: list[CredentialIn] = []


class ProviderSettingsOut(BaseModel):
    org_id: UUID
    job_id: UUID | None
    selection: ProviderSelection
    credentials: list[CredentialOut]


async def _organization(session: AsyncSession, context: AccountContext, org_id: UUID) -> Organization:
    organization = await session.scalar(
        select(Organization).where(
            Organization.id == org_id, Organization.account_id == context.account_id
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")
    return organization


async def _job(session: AsyncSession, org_id: UUID, job_id: UUID) -> Job:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return job


async def _settings_out(
    session: AsyncSession, org_id: UUID, job_id: UUID | None, config: dict | None
) -> ProviderSettingsOut:
    rows = (
        await session.scalars(
            select(ProviderCredential)
            .where(
                ProviderCredential.org_id == org_id,
                ProviderCredential.job_id.is_(None)
                if job_id is None
                else ProviderCredential.job_id == job_id,
            )
            .order_by(ProviderCredential.provider)
        )
    ).all()

    return ProviderSettingsOut(
        org_id=org_id,
        job_id=job_id,
        selection=ProviderSelection.model_validate(config or {}),
        credentials=[
            CredentialOut(
                provider=row.provider,
                base_url=row.base_url,
                key_last4=row.secret_last4,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
    )


async def _apply(
    session: AsyncSession,
    org_id: UUID,
    job_id: UUID | None,
    body: ProviderSettingsRequest,
) -> None:
    for entry in body.credentials:
        existing = await session.scalar(
            select(ProviderCredential).where(
                ProviderCredential.org_id == org_id,
                ProviderCredential.job_id.is_(None)
                if job_id is None
                else ProviderCredential.job_id == job_id,
                ProviderCredential.provider == entry.provider,
            )
        )

        if entry.api_key is None and existing is None:
            continue

        if entry.api_key == "":
            if existing is not None:
                await session.delete(existing)
            continue

        if existing is None:
            session.add(
                ProviderCredential(
                    org_id=org_id,
                    job_id=job_id,
                    provider=entry.provider,
                    base_url=entry.base_url,
                    secret_ciphertext=encrypt_secret(entry.api_key or ""),
                    secret_last4=last4(entry.api_key or ""),
                )
            )
        else:
            existing.base_url = entry.base_url
            if entry.api_key:
                existing.secret_ciphertext = encrypt_secret(entry.api_key)
                existing.secret_last4 = last4(entry.api_key)

    session.add(
        AuditEvent(
            org_id=org_id,
            entity_type="job" if job_id else "organization",
            entity_id=job_id or org_id,
            event_type="provider_settings_updated",
            payload={"providers": sorted(e.provider for e in body.credentials)},
        )
    )


@router.get("/tenants/{org_id}/provider-settings", response_model=ProviderSettingsOut)
async def read_org_providers(
    org_id: UUID,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    organization = await _organization(session, context, org_id)
    return await _settings_out(session, org_id, None, organization.provider_config)


@router.put("/tenants/{org_id}/provider-settings", response_model=ProviderSettingsOut)
async def write_org_providers(
    org_id: UUID,
    body: ProviderSettingsRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    organization = await _organization(session, context, org_id)
    organization.provider_config = body.selection.model_dump(mode="json", exclude_none=True)
    await _apply(session, org_id, None, body)
    await session.commit()

    return await _settings_out(session, org_id, None, organization.provider_config)


@router.get("/tenants/{org_id}/jobs/{job_id}/provider-settings", response_model=ProviderSettingsOut)
async def read_job_providers(
    org_id: UUID,
    job_id: UUID,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    await _organization(session, context, org_id)
    job = await _job(session, org_id, job_id)
    return await _settings_out(session, org_id, job_id, job.provider_config)


@router.put("/tenants/{org_id}/jobs/{job_id}/provider-settings", response_model=ProviderSettingsOut)
async def write_job_providers(
    org_id: UUID,
    job_id: UUID,
    body: ProviderSettingsRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    await _organization(session, context, org_id)
    job = await _job(session, org_id, job_id)
    job.provider_config = body.selection.model_dump(mode="json", exclude_none=True)
    await _apply(session, org_id, job_id, body)
    await session.commit()

    return await _settings_out(session, org_id, job_id, job.provider_config)
