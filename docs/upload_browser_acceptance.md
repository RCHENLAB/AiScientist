# Browser upload acceptance

The steps that check the browser → gateway half of an upload (tus), and the cards that follow it through the copy to HPC3.

## Local rehearsal

1. Start the gateway with a mock or test connection. Pick a small file, a nested folder, and a ZIP from the Data menu.
2. The browser starts an incremental SHA-256 worker alongside 8 MiB tus PATCH chunks with per-chunk checksums. It sends the whole-file digest with the final PATCH. At most three files transfer at once. The Datasets tab shows one card per file and one per folder: uploading, copying to HPC3, verifying, unpacking, done.
3. In DevTools, interrupt the network mid-transfer. The upload retries by itself for about five minutes; after that the card offers **Resume**. Verify HEAD reports the prior offset and the file completes without restarting. Click **Cancel** on another transfer and verify the `.part` file is removed.
   Then change the network path mid-transfer without closing the connection (turn the VPN on or off, switch Wi-Fi). The server hears nothing from the old path: it answers the browser's HEAD with 423 until it gives the stalled PATCH up, 30 s after its last byte, and the same upload continues. The card must not go back to 0 %; the account's `uploads/.upload.log` shows `busy` and `chunk_dropped` and no second `created` for the file.
4. Reload the page in the middle of an upload and of a folder. Each comes back as an "Interrupted" card; choosing the same file or folder again continues it, **Discard** removes what the server holds. A copy that failed comes back with **Retry copy** and **Dismiss**.
   Stop a file larger than 8 MiB part-way, then open another browser or a private window signed in to the same account. Before anything is chosen, the Datasets tab shows the file as "Interrupted at N% in another browser" (a folder: how many of its files are part-way); **Discard** there removes what the server holds. Choose the file there: it continues from where the server stopped (a toast gives the percentage), and the account's `uploads/.upload.log` shows `resume_found` and no second `created`. A different file given the same name, size and modification time starts from 0, with `resume_mismatch` in the log. A folder whose files stopped part-way continues in the same folder, not in `name_2/`.
5. Compare the uploaded file's SHA-256 with the local manifest. For a folder, verify nested paths and total file count. For an archive, choose unpack and verify the card ends as a folder.

Automated checks: `python -m pytest -q tests/test_tus_upload.py tests/test_tus_resume_js.py tests/test_upload_restore_js.py tests/test_upload_staging_api.py tests/test_resumable_upload.py` (`test_tus_resume_js.py` runs the page's resume hooks with the real tus-js-client under node, `test_upload_restore_js.py` the page's "Interrupted" cards); syntax: `node --check frontend/console/app.js`.

## Large fixtures

`scripts/generate_upload_test_data.py` prints a disk estimate by default. Pass `--execute` only on a machine with sufficient free space. The requested acceptance set is:

| Fixture | Generator |
| --- | --- |
| One 25 GiB file | `python scripts/generate_upload_test_data.py single --out <dir> --gb 25 --execute` |
| 25 GiB folder: one 10 GiB file and 5,000 other files | `python scripts/generate_upload_test_data.py folder --out <dir> --gb 25 --files 5000 --execute` |
| 20 GiB ZIP payload | `python scripts/generate_upload_test_data.py archive --out <dir> --gb 20 --execute` |

The archive generator keeps its payload beside the ZIP; budget about twice the requested archive size. Fixtures and manifests must stay outside Git.

## Eye server speed matrix

Run `scripts/upload_speed_probe.py` against the local gateway, from each network: campus wired, campus Wi-Fi, and VPN. For each network, use chunk sizes 8, 32, and 64 MiB and concurrency 1, 2, and 4. Supply at least as many input files as the chosen concurrency; the probe prints machine-readable JSON including aggregate MiB/s. Example:

```sh
python scripts/upload_speed_probe.py sample1.bin sample2.bin --url http://127.0.0.1:8000 --connection-id <id> --network wired --chunk-mb 32 --concurrency 2
```

If account login is enabled, set `UPLOAD_COOKIE` in the shell. Keep cookies out of logs. For every run, record network, file sizes, chunk size, concurrency, completion time, throughput, retries, and whether staging reached `done`.
