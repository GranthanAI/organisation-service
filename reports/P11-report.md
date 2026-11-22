# P11: organisation-service, finishing for the first college pilot, report

Commits: `P11a` (R1-R3), `P11b-P11d` (R4-R6), plus the dev-token alignment below. Nothing pushed.

## What changed
- **R1 payment-due seats.** `seat_rules.PAYMENT_DUE_STATUSES` (`past_due`, `suspended`) and `refuses_new_joins()`; joins are refused with `PAYMENT_DUE`
  while the seats are in those states; `payment_due` is exposed on the invite seats info and the CSV report.
- **R2 billing client.** `NoSubscription` only when the 404 problem body has `code == "NO_SUBSCRIPTION"`; any other 404 is `RouteMissing`
  (the word-guessing helper is gone). `Subscription.pending_seats` is read.
- **R3 pending seats.** Migration `0010_pending_seats` (`seat_state.pending_seats`, CHECK >= 0), stored by the billing seat source, cleared on end
  and by a staff set; shown as `seats.pending` on `GET /org/v1/me`.
- **R4 several processes.** Dockerfile runs `uvicorn --workers ${WEB_CONCURRENCY:-2}` with `PROMETHEUS_MULTIPROC_DIR`; `render_all()` uses the
  multiprocess collector, Gauges use `multiprocess_mode="max"`. Connection math is in README, lld.md and the launch checklist.
- **R5 test email.** `python -m scripts.send_test_email --to <address>` sends one mail through the configured SMTP sender.
- **R6 docs.** README, `docs/lld.md`, `docs/launch-checklist.md`, `docs/PHASES.md` updated.
- **Dev token.** `DEV_BILLING_TOKEN` default is now `dev-billing-token` (config and `.env.example`) so it matches the `billing:` entry of
  `INTERNAL_SERVICE_TOKENS` and billing's `ORG_SERVICE_TOKEN`. Your local `.env` (not committed) still has the old value: change it by hand.

## Tests (only touched files, as asked)
`test_billing_client` 12, `test_metrics_multiprocess`, `test_seat_rules` 31, `test_migrations` 14 (head `0010_pending_seats`),
`test_join_service`, `test_billing_seat_source`, api `test_invites_create`, `test_csv_upload`, `test_me`: passed. ruff clean.
**The full sweep was started and then stopped at your request; it has NOT been run for this phase.**

## Live check
100 concurrent joins against `uvicorn --workers 2` with 5 seats: 5 admitted, 95 `SEATS_FULL`, 5 joined events (p50 4.9 s).
The dev database was migrated to 0010.

## Notes
- The 20-concurrent latency target is not reachable on this Windows machine even for a route with no database (see P10 report).
- By hand: billing `ORG_SERVICE_URL` / `ORG_SERVICE_TOKEN`, auth `EMAIL_VERIFICATION_REQUIRED=true` before domain auto-join, one real test email,
  `FORWARDED_ALLOW_IPS` = gateway address.
