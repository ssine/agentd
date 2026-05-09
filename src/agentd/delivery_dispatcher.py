from __future__ import annotations

import logging
import threading
import time

from .channels.base import DeliveryRequest
from .channels.delivery import delivery_needs_queue
from .feishu import FeishuApi, final_message_card_width_mode, message_id_from_result
from .models import FeishuOutboxItem
from .registry import Registry

FEISHU_SEND_MIN_INTERVAL_SECONDS = 1


class DeliveryDispatcher:
    def __init__(
        self,
        *,
        registry: Registry,
        feishu: FeishuApi,
        log: logging.Logger,
        dry_send: bool = False,
        feishu_send_min_interval_seconds: float = FEISHU_SEND_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.registry = registry
        self.feishu = feishu
        self.log = log
        self.dry_send = dry_send
        self.feishu_send_min_interval_seconds = feishu_send_min_interval_seconds
        self._outbox_lock = threading.Lock()
        self.last_feishu_send_at = 0.0

    def dispatch(
        self,
        delivery: DeliveryRequest,
        *,
        run_id: int,
        replace_sent: bool = True,
    ) -> None:
        delivery_id = self.registry.upsert_delivery(
            channel=delivery.channel,
            destination_ref=delivery.destination_ref,
            thread_ref=delivery.thread_ref,
            kind=delivery.kind,
            dedupe_key=delivery.dedupe_key,
            payload=delivery.payload,
            run_id=run_id,
            state='queued' if delivery_needs_queue(delivery) else 'pending',
            replace_sent=replace_sent,
        )
        if not delivery_needs_queue(delivery):
            self.registry.mark_delivery_sent(delivery_id)
            if delivery.kind in {'final_state', 'final_text'}:
                self.registry.update_run(run_id, final_message_sent_at=int(time.time()))
            return
        self.registry.upsert_outbox(
            kind=delivery.kind,
            dedupe_key=delivery.dedupe_key,
            run_id=run_id,
            replace_sent=replace_sent,
            payload=delivery.payload,
        )

    def drain_feishu_outbox(self, *, limit: int = 20) -> None:
        if not self._outbox_lock.acquire(blocking=False):
            return
        try:
            for item in self.registry.claim_pending_outbox(limit):
                self.wait_for_feishu_send_slot()
                try:
                    remote_message_id = self._send_feishu_outbox_item(item)
                    if item.kind == 'status_card' and item.run_id is not None:
                        self.registry.mark_card_sent(
                            item.run_id,
                            remote_message_id=remote_message_id,
                            render_hash=str(item.payload.get('render_hash') or ''),
                        )
                    elif item.kind == 'final_reply' and item.run_id is not None:
                        self.registry.update_run(item.run_id, final_message_sent_at=int(time.time()))
                    self.registry.finish_outbox(item.id, sent=True)
                    delivery = self.registry.get_delivery_by_dedupe_key(item.dedupe_key)
                    if delivery is not None:
                        self.registry.mark_delivery_sent(delivery.id, external_ref=remote_message_id)
                except Exception as exc:
                    self.log.exception('failed to send Feishu outbox item %s', item.id)
                    if item.kind == 'status_card' and item.run_id is not None:
                        self.registry.mark_card_error(item.run_id, str(exc))
                    self.registry.finish_outbox(item.id, sent=False, error=str(exc))
                    delivery = self.registry.get_delivery_by_dedupe_key(item.dedupe_key)
                    if delivery is not None:
                        self.registry.mark_delivery_failed(delivery.id, str(exc))
                finally:
                    self.mark_feishu_send()
        finally:
            self._outbox_lock.release()

    def wait_for_feishu_send_slot(self) -> None:
        if self.dry_send:
            return
        wait_seconds = self.last_feishu_send_at + self.feishu_send_min_interval_seconds - time.monotonic()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def mark_feishu_send(self) -> None:
        self.last_feishu_send_at = time.monotonic()

    def _send_feishu_outbox_item(self, item: FeishuOutboxItem) -> str:
        payload = item.payload
        if item.kind == 'status_card':
            card = payload.get('card') if isinstance(payload.get('card'), dict) else {}
            action = str(payload.get('action') or 'create')
            message_id = str(payload.get('message_id') or '')
            if self.dry_send:
                print(str(payload.get('text') or ''))
                return message_id or 'dry-run-status'
            if action == 'update' and message_id:
                result = self.feishu.update_interactive(message_id, card)
                return message_id_from_result(result) or message_id
            if action == 'reply':
                result = self.feishu.reply_interactive(
                    str(payload.get('source_message_id') or ''),
                    card,
                    reply_in_thread=bool(payload.get('reply_in_thread')),
                )
            else:
                result = self.feishu.send_interactive(str(payload.get('chat_id') or ''), card)
            return message_id_from_result(result) or message_id

        if item.kind == 'final_reply':
            text = str(payload.get('text') or '')
            if self.dry_send:
                print(text)
                return ''
            width_mode = final_message_card_width_mode(text)
            session_kind = str(payload.get('session_kind') or '')
            if session_kind in {'main', 'schedule'}:
                result = self.feishu.send_markdown(str(payload.get('chat_id') or ''), text, width_mode=width_mode)
            else:
                result = self.feishu.reply_markdown(
                    str(payload.get('source_message_id') or ''),
                    text,
                    reply_in_thread=bool(payload.get('reply_in_thread')),
                    width_mode=width_mode,
                )
            return message_id_from_result(result)

        raise ValueError(f'unknown Feishu outbox kind: {item.kind}')
