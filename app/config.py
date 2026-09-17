"""Настройки дашборда. Все значения переопределяются переменными окружения."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DASH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Что считаем ---
    plan_calls_per_day: int = Field(
        default=120, description="План исходящих звонков на менеджера в день."
    )
    talk_threshold_sec: int = Field(
        default=15,
        description="Граница между «не дозвонился» и «поговорил». Звонки короче "
        "в план не засчитываются, но из статистики не выбрасываются: по ним видно, "
        "сколько сил уходит вхолостую.",
    )
    card_window_min: int = Field(
        default=30,
        description="Сколько минут после звонка правка карточки считается "
        "сделанной по этому звонку. Значение нужно согласовать с владельцем.",
    )
    order_window_hours: int = Field(
        default=24,
        description="Сколько часов после звонка заявка считается заведённой по "
        "этому звонку. Шире, чем окно карточки: менеджер сперва звонит, а заявку "
        "оформляет, когда договорится об условиях.",
    )
    workday_start_hour: int = Field(default=9, description="Начало рабочего дня, час.")
    workday_end_hour: int = Field(default=18, description="Конец рабочего дня, час.")
    timezone_offset_hours: int = Field(
        default=3,
        description="Сдвиг местного времени от UTC. ВАТС отдаёт время в UTC, "
        "а смотреть на дашборд будут по московскому.",
    )

    # --- Источник: ВАТС МегаФон ---
    vats_url: str = Field(
        default="https://vats247306.megapbx.ru/crmapi/v1",
        description="База REST API ВАТС. Ключ уходит заголовком X-API-KEY.",
    )
    vats_api_token: str | None = Field(
        default=None, description="Ключ ВАТС из кабинета. Пусто — сбор звонков выключен."
    )
    poll_interval_sec: int = Field(
        default=300, description="Как часто опрашивать ВАТС, секунд."
    )

    # --- Источник: Synergy CRM ---
    synergy_url: str = Field(default="https://app.synergycrm.ru/api/v1")
    synergy_api_token: str | None = Field(
        default=None, description="Токен Synergy. Пусто — проверка карточек выключена."
    )
    synergy_group: str = Field(
        default="Теплый прозвон",
        description="Группа пользователей Synergy, чьи звонки контролируем.",
    )
    # Идентификаторы кастомных полей карточки контакта. Их номера берутся из
    # кабинета Synergy и у каждой установки свои — по памяти не угадать.
    field_need: str | None = Field(
        default="custom-30493",
        description="«Какую технику привлекаете?» — им и проверяем потребность.",
    )
    field_objects: str | None = Field(
        default="custom-30375", description="«Есть объект»."
    )
    field_objects_extra: str | None = Field(
        default="custom-30492",
        description="«Сколько ведете объектов?» — засчитываем, если заполнено любое из двух.",
    )
    field_inn: str | None = Field(
        default="custom-29901", description="«инн-комп» в карточке контакта."
    )

    synergy_min_interval_sec: float = Field(
        default=0.35,
        description="Минимальный промежуток между запросами к Synergy. Лимит там "
        "общий на аккаунт и делится с сервисом распознавания, который ходит туда же.",
    )
    synergy_retries: int = Field(default=5, description="Повторов при ответе 429.")

    # --- Разбор разговоров ---
    asr_url: str = Field(
        default="http://127.0.0.1:8080",
        description="Сервис распознавания на этом же сервере.",
    )
    transcribe_min_duration_sec: int = Field(
        default=60,
        description="Не распознавать звонки короче. Короткие почти не несут "
        "содержания, а процессорное время съедают. 0 — распознавать все.",
    )
    transcribe_enabled: bool = Field(
        default=False,
        description="Разбор разговоров выключен по умолчанию: на 4 ядрах он "
        "не помещается в сутки при плане 120 звонков. Расчёт в PLAN.md.",
    )
    asr_timeout_sec: float = Field(
        default=900.0,
        description="Сколько ждать распознавание одной записи. Двухминутный "
        "разговор на модели small считается около полутора минут.",
    )
    records_dir: str = Field(
        default="data/records",
        description="Куда складывать скачанные записи разговоров.",
    )
    record_ssh_host: str | None = Field(
        default="root@91.220.109.49",
        description="Сервер, которому ВАТС отдаёт записи. Из Амстердама они "
        "недоступны: МегаФон пускает только российские адреса, поэтому запись "
        "скачивается там и приезжает сюда.",
    )
    record_ssh_key: str | None = Field(
        default="/home/agent/.ssh/ru-proxy_ed25519",
        description="Ключ к этому серверу. Читается только пользователем agent, "
        "поэтому скачивание записей запускается под ним, а разбор — под claude.",
    )
    openai_api_key: str | None = Field(
        default=None, description="Ключ OpenAI. Пусто — разбор расшифровок выключен."
    )
    analysis_model: str = Field(
        default="gpt-4o",
        description="Модель для разбора расшифровки. На сравнении gpt-4o-mini "
        "не вытаскивала из разговора ничего: возвращала «потребность не "
        "прозвучала» там, где клиент называл и технику, и объект.",
    )
    own_company: str = Field(
        default="Техно-Ресурс",
        description="Как называется наша компания. Нужно разбору: без этого "
        "модель принимает наше же название в речи клиента за конкурента.",
    )

    # --- Прочее ---
    db_path: str = Field(default="data/dashboard.db")
    demo_mode: bool = Field(
        default=True,
        description="Наполнять базу показательными данными, если настоящих "
        "источников нет. Демо-строки помечены и на дашборде видны как демо.",
    )
    log_level: str = "INFO"

    # Пустая строка в .env означает «не задано», иначе /health врёт о настройках.
    @field_validator(
        "vats_api_token", "synergy_api_token", "openai_api_key",
        "record_ssh_host", "record_ssh_key",
        "field_need", "field_objects", "field_objects_extra", "field_inn",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def vats_configured(self) -> bool:
        return bool(self.vats_api_token)

    @property
    def synergy_configured(self) -> bool:
        return bool(self.synergy_api_token)

    @property
    def analysis_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def card_fields_configured(self) -> bool:
        return all((self.field_need, self.field_objects, self.field_inn))


@lru_cache
def get_settings() -> Settings:
    return Settings()
