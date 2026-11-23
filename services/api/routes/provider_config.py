from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Account, AuditEvent, Job, Organization, Secret
from core.outbound_url import validate_base_url
from core.providers import SLOTS, catalogue, merge_selection
from core.schemas import ProviderSelection
from core.secrets import encrypt_secret, last4
from services.api.account_authz import AccountContext, owner_only

router = APIRouter(prefix="/accounts/{account_id}", tags=["providers"])
catalogue_router = APIRouter(tags=["providers"])

SLOT_LABELS = {
    "stt": "Speech-to-text",
    "tts": "Text-to-speech",
    "llm_call": "Conversation",
    "llm_summary": "Rubric scoring",
}


class SecretRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=4096)


class SecretUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    value: str | None = Field(default=None, min_length=1, max_length=4096)


class SecretOut(BaseModel):
    secret_id: UUID
    name: str
    last4: str
    used_by: list[str]
    created_at: datetime
    updated_at: datetime


class ProviderSettingsRequest(BaseModel):
    selection: ProviderSelection = ProviderSelection()

    @field_validator("selection")
    @classmethod
    def _check_base_urls(cls, value: ProviderSelection) -> ProviderSelection:
        for slot in SLOTS:
            validate_base_url(getattr(value, slot).base_url)
        return value


class ProviderSettingsOut(BaseModel):
    org_id: UUID | None
    job_id: UUID | None
    selection: ProviderSelection
    inherited: ProviderSelection | None = None


async def _organization(session: AsyncSession, context: AccountContext, org_id: UUID) -> Organization:
    organization = await session.scalar(
        select(Organization).where(
            Organization.id == org_id, Organization.account_id == context.account_id
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")
    return organization


async def account_references(session: AsyncSession, account_id: UUID) -> dict[UUID, list[str]]:
    account = await session.get(Account, account_id)
    organizations = (
        await session.scalars(select(Organization).where(Organization.account_id == account_id))
    ).all()
    org_ids = [organization.id for organization in organizations]
    jobs = (
        (await session.scalars(select(Job).where(Job.org_id.in_(org_ids)))).all() if org_ids else []
    )

    sources: list[tuple[dict | None, str | None]] = [
        (account.provider_config if account else None, None)
    ]
    sources += [(organization.provider_config, organization.name) for organization in organizations]
    sources += [(job.provider_config, job.title) for job in jobs]

    used: dict[UUID, list[str]] = {}
    for config, where in sources:
        selection = ProviderSelection.model_validate(config or {})
        for slot in SLOTS:
            secret_id = getattr(selection, slot).secret_id
            if secret_id is None:
                continue
            label = SLOT_LABELS[slot] if where is None else f"{SLOT_LABELS[slot]} · {where}"
            used.setdefault(secret_id, []).append(label)
    return used


async def check_secrets(session: AsyncSession, account_id: UUID, selection: ProviderSelection) -> None:
    wanted = {getattr(selection, slot).secret_id for slot in SLOTS} - {None}
    if not wanted:
        return
    found = set(
        (
            await session.scalars(
                select(Secret.id).where(Secret.account_id == account_id, Secret.id.in_(wanted))
            )
        ).all()
    )
    if wanted - found:
        raise HTTPException(status_code=404, detail="secret not found")


def _secret_out(secret: Secret, used: list[str]) -> SecretOut:
    return SecretOut(
        secret_id=secret.id,
        name=secret.name,
        last4=secret.last4,
        used_by=used,
        created_at=secret.created_at,
        updated_at=secret.updated_at,
    )


@router.get("/secrets", response_model=list[SecretOut])
async def list_secrets(
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> list[SecretOut]:
    used = await account_references(session, context.account_id)
    rows = (
        await session.scalars(
            select(Secret).where(Secret.account_id == context.account_id).order_by(Secret.name)
        )
    ).all()
    return [_secret_out(row, used.get(row.id, [])) for row in rows]


@router.post("/secrets", response_model=SecretOut, status_code=201)
async def create_secret(
    body: SecretRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> SecretOut:
    clash = await session.scalar(
        select(Secret).where(Secret.account_id == context.account_id, Secret.name == body.name)
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="a secret with that name already exists")

    secret = Secret(
        account_id=context.account_id,
        name=body.name,
        ciphertext=encrypt_secret(body.value),
        last4=last4(body.value),
    )
    session.add(secret)
    session.add(
        AuditEvent(
            org_id=None,
            entity_type="account",
            entity_id=context.account_id,
            event_type="secret_created",
            payload={"name": body.name, "last4": last4(body.value)},
        )
    )
    await session.commit()
    await session.refresh(secret)

    return _secret_out(secret, [])


@router.put("/secrets/{secret_id}", response_model=SecretOut)
async def update_secret(
    secret_id: UUID,
    body: SecretUpdate,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> SecretOut:
    secret = await session.scalar(
        select(Secret).where(Secret.id == secret_id, Secret.account_id == context.account_id)
    )
    if secret is None:
        raise HTTPException(status_code=404, detail="not found")

    if body.name is not None and body.name != secret.name:
        clash = await session.scalar(
            select(Secret).where(Secret.account_id == context.account_id, Secret.name == body.name)
        )
        if clash is not None:
            raise HTTPException(status_code=409, detail="a secret with that name already exists")
        secret.name = body.name

    if body.value is not None:
        secret.ciphertext = encrypt_secret(body.value)
        secret.last4 = last4(body.value)

    await session.commit()
    await session.refresh(secret)
    used = await account_references(session, context.account_id)

    return _secret_out(secret, used.get(secret.id, []))


@router.delete("/secrets/{secret_id}", status_code=204)
async def delete_secret(
    secret_id: UUID,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> None:
    secret = await session.scalar(
        select(Secret).where(Secret.id == secret_id, Secret.account_id == context.account_id)
    )
    if secret is None:
        raise HTTPException(status_code=404, detail="not found")

    used = (await account_references(session, context.account_id)).get(secret_id, [])
    if used:
        raise HTTPException(status_code=409, detail=f"still used by {', '.join(used)}")

    await session.delete(secret)
    await session.commit()


@router.get("/provider-settings", response_model=ProviderSettingsOut)
async def read_account_providers(
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    account = await session.get(Account, context.account_id)
    return ProviderSettingsOut(
        org_id=None,
        job_id=None,
        selection=ProviderSelection.model_validate((account.provider_config if account else None) or {}),
    )


@router.put("/provider-settings", response_model=ProviderSettingsOut)
async def write_account_providers(
    body: ProviderSettingsRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    account = await session.get(Account, context.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="not found")
    await check_secrets(session, context.account_id, body.selection)

    account.provider_config = body.selection.model_dump(mode="json", exclude_none=True)
    session.add(
        AuditEvent(
            org_id=None,
            entity_type="account",
            entity_id=context.account_id,
            event_type="provider_settings_updated",
            payload={"scope": "account"},
        )
    )
    await session.commit()

    return ProviderSettingsOut(
        org_id=None,
        job_id=None,
        selection=ProviderSelection.model_validate(account.provider_config or {}),
    )


@router.get("/tenants/{org_id}/provider-settings", response_model=ProviderSettingsOut)
async def read_org_providers(
    org_id: UUID,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    organization = await _organization(session, context, org_id)
    account = await session.get(Account, context.account_id)
    return ProviderSettingsOut(
        org_id=org_id,
        job_id=None,
        selection=ProviderSelection.model_validate(organization.provider_config or {}),
        inherited=ProviderSelection.model_validate(
            (account.provider_config if account else None) or {}
        ),
    )


@router.put("/tenants/{org_id}/provider-settings", response_model=ProviderSettingsOut)
async def write_org_providers(
    org_id: UUID,
    body: ProviderSettingsRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> ProviderSettingsOut:
    organization = await _organization(session, context, org_id)
    await check_secrets(session, context.account_id, body.selection)

    organization.provider_config = body.selection.model_dump(mode="json", exclude_none=True)
    session.add(
        AuditEvent(
            org_id=org_id,
            entity_type="organization",
            entity_id=org_id,
            event_type="provider_settings_updated",
            payload={"scope": "organization"},
        )
    )
    await session.commit()

    return ProviderSettingsOut(
        org_id=org_id,
        job_id=None,
        selection=ProviderSelection.model_validate(organization.provider_config or {}),
    )


@catalogue_router.get("/provider-catalogue")
async def read_catalogue() -> dict:
    return catalogue()
