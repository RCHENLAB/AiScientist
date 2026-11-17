# Images in the chat, and the file preview

RCHENLAB/AiScientist issue 115. Code: `frontend/console/fileview.js` (the preview by file type),
`frontend/console/app.js` (composer, messages, Markdown links), `src/aiscientist/gateway/chat_images.py`
(storage and model support), `gateway/auth_routes.py` (upload, serve) and `gateway/app.py` (the run path).

## The preview, by file type

One viewer for every place a file is opened: the Results panel's Preview tab, the dataset preview and the
Files page. A file's text is read as a stream and stops at 1 MB, so a table of several GB opens as fast as a
small one; a cut file says so ("Showing the first 1.0 MB of 40.0 MB") and every preview keeps its Download
button.

| Type | Preview |
|---|---|
| csv, tsv | A table: sticky header and row numbers, a type under each column name (integer, number, boolean, text), horizontal scroll, "N columns, M rows" and "rows loaded of M" when cut, "Show 200 more rows". Click a header to sort; Enter works on a focused header. |
| md | Rendered Markdown (headings, tables, code, lists, links, images) with a Rendered / Source switch. A relative image path resolves inside the run folder only. |
| json | Pretty-printed and highlighted, folds by object and array, Expand all / Collapse all. Text that is not complete JSON (a cut file) is shown as text. |
| yaml | Highlighted (keys, comments, strings, numbers, booleans, null), with line numbers. |
| html | Source only, with line numbers; never executed. |
| other text | The text with line numbers (the numbers are not copied with it). |
| image, pdf | Shown inside the panel; an image opens full size on click. |

Sorting works on the rows that were loaded, not on a server-side query: for a file longer than the part read,
the view says "sorted within the loaded rows".

Nothing in a file is ever inserted as markup: cells, keys, values and names go through one escaping function,
and a run's `.html`, `.svg` and `.xml` files are served with a sandboxing Content-Security-Policy and
`X-Content-Type-Options: nosniff`, so opening one by link runs no script on the console's origin.

## Links and images in Markdown

`[text](url)` is drawn as a link only for a URL the page may follow: the account's own run files
(`/api/file/<owner>/<run>/…`), its dataset files (`/api/datasets/<id>/download`), its chat images, or an
`https://` address. `![alt](url)` is drawn as an image only for the account's own files; an image on an
`https://` address is shown as a link to it. A reply is model text, and an image is fetched as soon as the
reply shows, so an address carrying something from the chat would reach another site without a click.
Anything else (`javascript:`, `data:`, `http:`, another origin, `//host`, `..` in an absolute path, a
relative path that leaves the run folder) is shown as plain text. No raw HTML is run.

## Images in a message

Attach (the **Image** button), paste from the clipboard or drop a PNG, JPEG, WebP or GIF on the chat. It is
uploaded to the conversation at once, so **no HPC3 connection is needed**, and shows as a thumbnail that
can be removed before sending. Limits: 5 images per message, 7.5 MB each (the Claude API takes 10 MB per
image once it is base64-encoded, which is a third longer), and 200 MB of chat images per account. The sent
message shows the thumbnails (a click opens one full size), and they are still there after a reload.

* **Checked by content.** The server accepts a file only if its first bytes are a PNG, JPEG, WebP or GIF, whatever
  its name or type says. SVG and everything else are refused.
* **Where they live.** One file per image under the account: `<data root>/users/<account>/chat-images/<chat>/`
  (or `<results dir>/<account>/chat-images/<chat>/` with no data root). They belong to their conversation and are
  **not datasets**, so the 90-day dataset retention does not apply; deleting the chat deletes its images. An image no
  message names is deleted when its thumbnail is removed from the composer, and a day after its upload otherwise (the
  account's next upload removes it). A message stores references (id, name, type, size, url), never the bytes, and the
  server rebuilds them from what it holds, not from what the page sent.
* **Who can read them.** Only the conversation's owner (`GET /api/chat-images/<chat>/<id>.<ext>`), shown as the checked
  image type with `nosniff` and a sandboxing policy.

### What the model sees

A message with images and no dataset attached goes to the fast chat, which adds the images to the question's
message only (the history stays text). OpenAI-compatible endpoints get `image_url` parts with a data URL;
a native Anthropic key gets Messages-API image blocks. A model that does not accept images is told so, not
skipped:

* the composer asks the server (`GET /api/lab/vision`) and says "This session's model (…) cannot read images, so a
  message with them will not be sent" next to the thumbnails;
* the message is not sent, and the server refuses it with a 409 if a client sends it anyway; the text and the
  images are given back to the composer.

Which models read images: a native Anthropic key always does; any other endpoint by its model's name (gpt-4o,
gpt-4.1, gpt-5, gemini, claude, llava, pixtral, gemma-3, `-vl` models, …). Set `AISCIENTIST_VISION_MODELS` to
correct that for a deployment: a comma list of model-name fragments, `all` or `none`. The lab's shared model
does not accept images unless it is listed.

An analysis run reads text and data, not images, so a message that attaches both a dataset and an image is
refused with a message saying so (send the image in a chat with no dataset attached, or remove it).

## A run's figures in its reply

The reply that lists a run's results shows the first four images the run wrote (those under `figures/` first) as
thumbnails with their names, each opening full size, and "N more figures in Results" when there are more.
