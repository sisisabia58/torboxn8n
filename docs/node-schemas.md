# n8n node parameter reference

Verified against n8n `master` source (`packages/nodes-base/nodes/...`) on 2026-08-17.
Nothing here is from memory. n8n silently ignores unknown parameter keys, so every
value below was read from the node's own `description` array or execute function.

Only the nodes and operations this project needs are documented.

---

## Telegram Trigger

`n8n-nodes-base.telegramTrigger` · typeVersion `1.5` · credential `telegramApi`

```json
{ "updates": ["message"], "additionalFields": {} }
```

`updates` is multiOptions: `*`, `message`, `edited_message`, `channel_post`,
`edited_channel_post`, `callback_query`, `inline_query`, `poll`,
`pre_checkout_query`, `shipping_query`.

---

## Telegram — send message

`n8n-nodes-base.telegram` · typeVersion `1.2` · credential `telegramApi`

```json
{
  "resource": "message",
  "operation": "sendMessage",
  "chatId": "={{ $json.message.chat.id }}",
  "text": "Queued.",
  "additionalFields": { "appendAttribution": false }
}
```

`appendAttribution` defaults to **true** — it appends an n8n promo line to every
message. Set it false explicitly.

## Telegram — edit message text

```json
{
  "resource": "message",
  "operation": "editMessageText",
  "messageType": "message",
  "chatId": "={{ ... }}",
  "messageId": "={{ ... }}",
  "text": "...",
  "additionalFields": {}
}
```

`messageType: "message"` gates `chatId` + `messageId` (the alternative,
`inlineMessage`, uses `inlineMessageId` instead). `disable_notification` is **not
valid** for `editMessageText` — hidden via `displayOptions.hide`.

---

## If (v2)

`n8n-nodes-base.if` · typeVersion `2.2` · no credential · outputs `["true", "false"]`

The `conditions` parameter is an object with **three sibling keys** —
`combinator`, `conditions`, and its own nested `options`. The nested
`conditions.options` is distinct from the node-level `options`.

```json
{
  "conditions": {
    "combinator": "and",
    "options": {
      "caseSensitive": true,
      "leftValue": "",
      "typeValidation": "strict",
      "version": 2
    },
    "conditions": [
      {
        "id": "c1",
        "leftValue": "={{ $json.message.text }}",
        "rightValue": "https?://mega\\.nz/(folder|file)/\\S+",
        "operator": { "type": "string", "operation": "regex" }
      }
    ]
  },
  "looseTypeValidation": false,
  "options": {}
}
```

**Boolean is-true** condition (note `singleValue`, and `rightValue` unused):

```json
{
  "id": "c1",
  "leftValue": "={{ $json.data.download_finished }}",
  "rightValue": "",
  "operator": { "type": "boolean", "operation": "true", "singleValue": true }
}
```

`conditions.options.version` is the **filter schema** version, not the node's:
node typeVersion ≥ 2.3 → `3`; ≥ 2.2 → `2`; ≤ 2.1 → `1`.
(`IfV2.node.ts`: `version: '={{ $nodeVersion >= 2.3 ? 3 : $nodeVersion >= 2.2 ? 2 : 1 }}'`)

---

## Wait

`n8n-nodes-base.wait` · typeVersion `1.1` · no credential

```json
{ "resume": "timeInterval", "amount": 30, "unit": "seconds" }
```

`amount` and `unit` are **top-level**, not nested in options. The node also needs a
`webhookId` (a UUID) at node level.

---

## HTTP Request

`n8n-nodes-base.httpRequest` · typeVersion `4.2` · credential `httpHeaderAuth`

```json
{
  "method": "POST",
  "url": "https://api.torbox.app/v1/api/integration/googledrive",
  "authentication": "genericCredentialType",
  "genericAuthType": "httpHeaderAuth",
  "sendHeaders": true,
  "specifyHeaders": "keypair",
  "headerParameters": {
    "parameters": [{ "name": "User-Agent", "value": "n8n-torbox-workflow/1.0" }]
  },
  "sendBody": true,
  "contentType": "json",
  "specifyBody": "json",
  "jsonBody": "={{ JSON.stringify({ id: 1, type: 'webdownload' }) }}"
}
```

Form-urlencoded body (used for the Google token mint) instead uses
`"contentType": "form-urlencoded"` with `bodyParameters.parameters[]` of
`{name, value}` — there is no `specifyBody` in that mode.

---

## Code

`n8n-nodes-base.code` · typeVersion `2` · no credential

```json
{ "mode": "runOnceForAllItems", "language": "javaScript", "jsCode": "return [];" }
```

Python uses key `pythonCode` instead of `jsCode`.

---

## Loop Over Items (Split In Batches)

`n8n-nodes-base.splitInBatches` · typeVersion `3` · outputs `["done", "loop"]`

```json
{ "batchSize": 10, "options": { "reset": false } }
```

`batchSize` is **top-level**, not inside `options`.

---

## Google Drive — search

`n8n-nodes-base.googleDrive` · typeVersion `3` · credential `googleDriveOAuth2Api`

```json
{
  "authentication": "oAuth2",
  "resource": "fileFolder",
  "operation": "search",
  "searchMethod": "query",
  "queryString": "name = 'x' and trashed = false",
  "returnAll": false,
  "limit": 10,
  "filter": { "whatToSearch": "files", "includeTrashed": false }
}
```

Resource is `fileFolder` (not `file`) for search.

## Google Drive — rename

```json
{
  "authentication": "oAuth2",
  "resource": "file",
  "operation": "update",
  "fileId": { "__rl": true, "mode": "id", "value": "={{ $json.id }}" },
  "newUpdatedFileName": "={{ ... }}",
  "options": {}
}
```

> **`file:update` cannot move a file.** Its execute function PATCHes only `name`
> and binary content — it never touches `parents`. There is no `addParents`
> parameter on this operation. Moving requires the separate `move` operation below.

## Google Drive — move

```json
{
  "authentication": "oAuth2",
  "resource": "file",
  "operation": "move",
  "fileId": { "__rl": true, "mode": "id", "value": "={{ $json.id }}" },
  "folderId": { "__rl": true, "mode": "id", "value": "<target folder id>" },
  "driveId": { "__rl": true, "mode": "list", "value": "My Drive" }
}
```

Execute does `GET /files/{id}?fields=parents`, then PATCHes with
`addParents` = target and `removeParents` = the existing parents joined by comma.
For an **orphaned** file (no parents) `removeParents` resolves to `''`, which is safe.

---

## Google Sheets — append row

`n8n-nodes-base.googleSheets` · typeVersion `4.5` · credential `googleSheetsOAuth2Api`

```json
{
  "authentication": "oAuth2",
  "resource": "sheet",
  "operation": "append",
  "documentId": { "__rl": true, "mode": "id", "value": "<spreadsheet id>" },
  "sheetName": { "__rl": true, "mode": "name", "value": "Sheet1" },
  "columns": {
    "mappingMode": "defineBelow",
    "value": { "timestamp": "={{ $now.toISO() }}", "outcome": "success" }
  },
  "options": {}
}
```

`columns` is a resourceMapper (typeVersion ≥ 4). Older versions use
`dataMode` + `fieldsUi.fieldValues[]` instead.

---

## resourceLocator shape

Several parameters above are resourceLocators. The object is always:

```json
{ "__rl": true, "mode": "id" | "list" | "url" | "name", "value": "..." }
```

Passing a bare string where a resourceLocator is expected is a silent failure.
