# Chat groups and chat titles

Code: `frontend/console/app.js` (the chat list, its menu, drag and drop), `src/aiscientist/gateway/auth_routes.py`
(the API), `gateway/chat_titles.py` (the title), `gateway/app.py` (`_name_chat`) and
`gateway/models.py` (`ConversationGroup`, `Conversation.group_id`, `Conversation.title_source`; migration 0009).

## Groups

A signed-in user can file chats under groups of their own. With no group the list is flat. With at
least one, each group has a head that folds it, and chats in no group are listed under "Other chats".

| Action | In the console |
| --- | --- |
| New group | the folder button beside "+" in the Chats head, or "New group…" in a chat's ⋯ menu |
| File a chat | drag it onto a group, or pick the group in its ⋯ menu |
| Take a chat out | drag it onto "Other chats", or pick "Other chats (no group)" |
| Rename, delete a group | the buttons on its head; deleting a group keeps its chats |

Groups belong to the account and are the same on every browser. Which groups are folded is kept in
the browser.

| Endpoint | What it does |
| --- | --- |
| `GET /api/conversations` | the chats (each with `group_id`, `title_source`) and `groups` in their order |
| `POST /api/conversation-groups` | body `{"name": ...}`; a new group at the end |
| `PATCH /api/conversation-groups/{gid}` | body `{"name": ...}` |
| `DELETE /api/conversation-groups/{gid}` | removes the group; its chats become ungrouped |
| `PATCH /api/conversations/{cid}` | `{"group_id": <id> \| null}` files the chat or takes it out |

## Titles

A new chat is first titled by the page with the opening words of its first message, after the name of
the attached data. The gateway then asks the session's model (role `classify`) to name what the
request asks for in at most eight words, stores `<data> · <title>` and tells the page (`chat_title`
on the session's stream). Two chats of one account with the same title are told apart by the time
the chat was made.

`title_source` records where the title came from, and decides what may replace it:

| `title_source` | Set by | Replaced by |
| --- | --- | --- |
| `prompt` | the page, from the first message | the model's title, or the user's |
| `intent` | the session's model | the user's only |
| `user` | the user (double-click the title, or "Rename chat…") | nothing |

A chat is named by the model once, at its first request. When the session's model is not up at that
moment, or the call fails, the page's title stays.
