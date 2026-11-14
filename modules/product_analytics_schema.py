"""Explicit analytics wire contract. No SDK, secrets, DB, requests or implicit capture.

Keep in sync with frontend src/analytics/productPolicy.js. This module alone
does not enable analytics. Client events are untrusted behavioral reports;
successful writes and paid conversions may only be emitted by backend code.
"""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

POLICY_VERSION = 'posthog-v1'
ROUTE_KEYS = frozenset(('home', 'pricing', 'authorization', 'chart_creation',
    'chart_view', 'my_charts', 'relationship_creation', 'relationship_view',
    'daily_horoscope', 'terms', 'privacy', 'refunds', 'support'))
PROVIDERS = ('stripe', 'lava')
ERRORS = ('network', 'unavailable', 'access_denied', 'quota', 'invalid_input', 'cancelled', 'unknown')
SUBJECTS = ('natal', 'relationship')
LATENCIES = ('under_5s', '5_to_15s', '15_to_30s', 'over_30s')
WHEEL_OBJECTS = ('point', 'house', 'aspect')  # kind of chart object only, never which planet/sign/house
PRODUCTS = ('premium_month', 'premium_year', 'question_pack')  # names only, never prices or ids
CLIENT_EVENTS = {
    'page_viewed': {}, 'page_leave_hint': {},
    'landing_cta_clicked': {'target': ('chart', 'relationships', 'pricing', 'login')},
    'landing_demo_selected': {},
    'landing_scroll_reached': {'milestone': ('25', '50', '75', '90')},
    'engagement_milestone': {'milestone': ('30s', '60s')},
    'registration_started': {'method': ('email', 'google')},
    'chart_creation_started': {}, 'chart_creation_failed': {'reason': ERRORS},
    'relationship_creation_started': {}, 'relationship_creation_failed': {'reason': ERRORS},
    'ai_question_started': {'subject': SUBJECTS},
    'ai_answer_presented': {'subject': SUBJECTS, 'latency': LATENCIES},
    'ai_answer_failed': {'subject': SUBJECTS, 'reason': ERRORS},
    'ai_followup_selected': {'subject': SUBJECTS},
    'premium_offer_viewed': {}, 'payment_provider_selected': {'provider': PROVIDERS},
    'checkout_initiation_failed': {'provider': PROVIDERS, 'reason': ERRORS},
    'billing_portal_opened': {'provider': PROVIDERS},
    'wheel_tooltip_opened': {'object': WHEEL_OBJECTS},
    'wheel_question_selected': {'object': WHEEL_OBJECTS},
    'site_error_occurred': {'kind': ('render', 'script', 'request')},
    'offer_moment_shown': {'moment': ('last_free_question', 'question_limit', 'chart_limit')},
    'offer_option_selected': {'option': PRODUCTS},
}
SERVER_EVENTS = {
    'registration_completed': {'method': ('email', 'google')},
    'email_verification_completed': {},
    'login_succeeded': {'method': ('email', 'google')},
    'password_reset_completed': {}, 'chart_creation_completed': {'actor': ('guest', 'account')},
    'relationship_creation_completed': {},
    'ai_answer_completed': {'subject': SUBJECTS, 'latency': LATENCIES},
    'feedback_submitted': {'category': ('idea', 'bug', 'feedback')},
    'checkout_initiated': {'provider': PROVIDERS, 'product': PRODUCTS},
    'subscription_started': {'provider': PROVIDERS},
    'billing_period_paid': {'provider': PROVIDERS},
    'subscription_cancel_scheduled': {'provider': PROVIDERS},
    'subscription_expired': {'provider': PROVIDERS},
    'guest_chart_saved': {'method': ('email', 'google')},
    'payment_failed': {'provider': PROVIDERS, 'stage': ('checkout', 'first_payment', 'renewal', 'confirmation')},
    'free_limit_would_block': {'resource': ('chart', 'question')},
    'question_pack_purchased': {'provider': PROVIDERS},
    'annual_plan_started': {'provider': PROVIDERS},
}


def checked_properties(name, properties, *, source):
    schema = (CLIENT_EVENTS if source == 'client' else SERVER_EVENTS if source == 'server' else {}).get(name)
    if schema is None or not isinstance(properties, dict) or set(properties) != set(schema):
        raise ValueError('Invalid analytics event.')
    if any(not isinstance(value, str) or value not in schema[key] for key, value in properties.items()):
        raise ValueError('Invalid analytics properties.')
    return {key: properties[key] for key in schema}


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: UUID4
    session_id: UUID4
    name: str = Field(strict=True, max_length=48)
    route_key: str = Field(strict=True, max_length=32)
    viewport_class: Literal['small', 'medium', 'large', 'unknown'] = 'unknown'
    campaign_source: Literal['unspecified', 'telegram', 'instagram', 'vk', 'youtube', 'other'] = 'unspecified'
    properties: dict = Field(default_factory=dict, max_length=3)

    @field_validator('route_key')
    @classmethod
    def route_allowlist(cls, value):
        if value not in ROUTE_KEYS:
            raise ValueError('Invalid analytics page.')
        return value

    @model_validator(mode='after')
    def properties_allowlist(self):
        self.properties = checked_properties(self.name, self.properties, source='client')
        return self


class ClientBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy_version: Literal['posthog-v1']
    visitor_id: UUID4
    events: list[ClientEvent] = Field(min_length=1, max_length=20)

    @field_validator('events')
    @classmethod
    def unique_events(cls, values):
        seen: set[UUID] = set()
        for value in values:
            if value.event_id in seen:
                raise ValueError('Duplicate analytics event.')
            seen.add(value.event_id)
        return values
