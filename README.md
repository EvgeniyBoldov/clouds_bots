# eXpress Bot Gateway

FastAPI/PostgreSQL gateway for eXpress BotX. Register bot accounts in the admin page; credentials are stored in PostgreSQL and loaded into pybotx's runtime account provider. Secrets are never returned by the admin API. User grants are scoped to a bot. Group mentions are callable by any participant; grants restrict personal bot access.

## Run

```sh
cp .env.example .env
docker compose up --build
```

Open `/` for the admin page and sign in with `GATEWAY_ADMIN_TOKEN`. The admin page uses a 12-hour HttpOnly session cookie. Set `GATEWAY_ADMIN_COOKIE_SECURE=true` when HTTPS terminates before this service so browsers send the cookie only over HTTPS; it defaults to `false` for the included HTTP Compose setup. Existing API integrations can continue using `X-Admin-Token`. Health endpoints: `/health/`, `/health/ready`. The authorized notification endpoint is `POST /api/v1/notifications`.

`GATEWAY_BOT_ACCOUNTS_JSON` is supported only as a legacy startup import. Add new bot accounts from the admin page with the `bot_id`, `cts_url`, `secret_key`, and name issued when the bot is registered in the eXpress admin console.

## pybotx integration status

`src/gateway/bot.py` uses pybotx's `BotAccountProvider` snapshot so newly registered credentials apply without restarting the process. It persists authorized personal messages, deduplicates events, answers personal chats, and routes group messages to the selected template, with default replies for structured contact mentions, replies to the bot, and slash commands. Group calls are open to all users. Use `allowed_data=all` in eXpress admin.

The main app mounts the documented pybotx routes and runs a PostgreSQL polling worker that sends queued notification messages via `Bot.send_message`. `gateway.bot` supplies the shared collector and Bot instance.

## Bot behavior templates

Each database bot account stores a `template_id`. The admin page lists the templates registered by trusted application code and binds one to each BotX account. The incoming BotX request is authenticated by pybotx; the worker uses the verified bot id to load its database record and dispatches the message to that record's template. The admin API accepts only a registered template id and cannot import arbitrary Python modules.

The built-in `echo` template replies to ordinary text, provides `/help`, and answers `/ping` with `pong`. To add behavior, create a module such as `src/gateway/custom_templates.py`:

```python
from gateway.integrations import HandlerRequest, HandlerResult
from gateway.templates import BaseBot, command, register_template


@register_template(
    "welcome",
    name="Welcome Bot",
    description="Отвечает на приветствие.",
)
class WelcomeBot(BaseBot):
    @command("/hello", "Поздороваться")
    async def hello(self, request: HandlerRequest) -> HandlerResult:
        return HandlerResult(text=f"Привет, {request.user_id}!")
```

Import the module from `src/gateway/main.py` after `gateway.templates` is imported:

```python
from . import custom_templates  # runs the explicit template registrations
```

Then rebuild/redeploy the app. The registered class and commands appear in `GET /api/v1/admin/bot-templates`; select the template on a BotX account in the admin page. Template IDs are stable bindings stored in PostgreSQL, while the code implementation comes from the currently deployed registry. If a selected id is missing after deployment, the bot list reports `template_status: "unavailable"` and messages receive an explicit unavailable response until an administrator selects an available template.

Commands inherit from base classes. A decorated method with the same command name replaces the inherited command; an ordinary Python method override can replace its inherited implementation. `/summary` keeps the legacy history summary behavior when the selected template does not declare that command. A template that explicitly declares `/summary` takes precedence; the legacy fallback remains group-only and honors the chat history setting.

## Permissions

The admin UI grants a user `use` access per bot. Email addresses resolve through BotX to the user's stable HUID; an AD account must include both AD login and AD domain; HUID can also be provided directly. Personal messages are accepted only from users with a matching grant. Group invocation policy remains open by design per the project requirement. Never expose the admin API directly without a strong token and TLS.

Production release and deployment instructions: [docs/deployment.md](docs/deployment.md).

Group message delivery depends on the eXpress bot card: add the bot to the chat and set the available message types (`allowed_data`) to `all`. The gateway stores received group messages when chat history is enabled. Templates can override `on_group_message` to inspect ordinary group messages (it returns nothing by default), `on_mention` for structured bot mentions, and `on_reply` for replies whose `reply.author_id` matches the bot ID; decorated commands take priority. `HandlerRequest.metadata` contains `is_mentioned`, `is_reply_to_bot`, and the reply's author, message ID and body. Direct messages continue to use `on_message`. The platform only delivers messages permitted by its bot settings; it does not provide earlier chat history through this handler.
