from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import settings
from app.keyboards.inline import (
    _build_cabinet_main_menu_keyboard,
    _should_show_cabinet_purchase_button,
    get_main_menu_keyboard,
)
from app.localization.texts import get_texts


@pytest.mark.parametrize(
    ('has_active_subscription', 'subscription_is_active', 'subscription', 'expected'),
    [
        (False, False, None, True),
        (True, True, SimpleNamespace(is_trial=True), True),
        (True, False, SimpleNamespace(is_trial=False), True),
        (True, True, SimpleNamespace(is_trial=False), False),
    ],
    ids=['no-subscription', 'active-trial', 'expired-paid', 'active-paid'],
)
def test_should_show_purchase_button_only_without_active_paid_subscription(
    has_active_subscription: bool,
    subscription_is_active: bool,
    subscription,
    expected: bool,
) -> None:
    assert (
        _should_show_cabinet_purchase_button(
            has_active_subscription=has_active_subscription,
            subscription_is_active=subscription_is_active,
            subscription=subscription,
        )
        is expected
    )


def test_purchase_button_opens_cabinet_home_below_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com', raising=False)
    layout = {
        'row_1': {'buttons': ['home'], 'max_per_row': 1},
        'row_2': {'buttons': ['custom_news'], 'max_per_row': 1},
        'custom_buttons': {
            'custom_news': {
                'enabled': True,
                'url': 'https://t.me/example',
                'labels': {'ru': 'Новости'},
                'open_in': 'external',
            }
        },
    }
    styles = {'home': {'enabled': True, 'labels': {'ru': 'Открыть кабинет'}}}

    with (
        patch('app.utils.menu_layout_cache.get_cached_menu_layout', return_value=layout),
        patch('app.utils.button_styles_cache.get_cached_button_styles', return_value=styles),
    ):
        keyboard = _build_cabinet_main_menu_keyboard(
            'ru',
            get_texts('ru'),
            is_admin=False,
            is_moderator=False,
            show_purchase_button=True,
        )

    rows = keyboard.inline_keyboard
    assert [row[0].text for row in rows] == [
        'Открыть кабинет',
        get_texts('ru').MENU_BUY_SUBSCRIPTION,
        'Новости',
    ]
    assert rows[1][0].web_app is not None
    assert rows[1][0].web_app.url == 'https://cabinet.example.com'
    assert rows[1][0].style == 'primary'


def test_purchase_button_is_absent_for_active_paid_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com', raising=False)
    layout = {
        'row_1': {'buttons': ['home'], 'max_per_row': 1},
        'custom_buttons': {},
    }

    with (
        patch('app.utils.menu_layout_cache.get_cached_menu_layout', return_value=layout),
        patch('app.utils.button_styles_cache.get_cached_button_styles', return_value={}),
    ):
        keyboard = _build_cabinet_main_menu_keyboard(
            'ru',
            get_texts('ru'),
            is_admin=False,
            is_moderator=False,
            show_purchase_button=False,
        )

    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].web_app.url == 'https://cabinet.example.com'


@pytest.mark.parametrize(
    ('has_active_subscription', 'subscription_is_active', 'subscription', 'expected_rows'),
    [
        (False, False, None, 2),
        (True, True, SimpleNamespace(is_trial=False), 1),
    ],
    ids=['public-no-subscription', 'public-active-paid'],
)
def test_public_main_menu_wires_purchase_visibility(
    monkeypatch: pytest.MonkeyPatch,
    has_active_subscription: bool,
    subscription_is_active: bool,
    subscription,
    expected_rows: int,
) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com', raising=False)
    layout = {
        'row_1': {'buttons': ['home'], 'max_per_row': 1},
        'custom_buttons': {},
    }

    with (
        patch('app.utils.menu_layout_cache.get_cached_menu_layout', return_value=layout),
        patch('app.utils.button_styles_cache.get_cached_button_styles', return_value={}),
    ):
        keyboard = get_main_menu_keyboard(
            has_active_subscription=has_active_subscription,
            subscription_is_active=subscription_is_active,
            subscription=subscription,
        )

    assert len(keyboard.inline_keyboard) == expected_rows
