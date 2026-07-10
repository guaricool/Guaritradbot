<?php
// Manual deploy trigger via Coolify local API.
//
// Token: read from COOLIFY_TOKEN environment variable. NEVER hardcode it
// in this file (the repo is public on GitHub and tokens get scraped within
// hours of being committed).
//
// Usage:
//   1. Set COOLIFY_TOKEN in your shell: `export COOLIFY_TOKEN=...`
//   2. Run: `php .deploy.php`
//
// NOTE: this file is a development trigger only. For production deploys,
// use Coolify's built-in webhook on push to the main branch. Also note
// this script does NOT add any auth of its own — if it ever ends up in
// an HTTP-served directory, anyone with the URL can trigger a deploy.
// (See audit finding C1/L9.)
$tok = getenv("COOLIFY_TOKEN");
if ($tok === false || $tok === "") {
    fwrite(STDERR, "ERROR: COOLIFY_TOKEN env var is required. Set it in your shell.\n");
    exit(1);
}
$ctx = stream_context_create([
    "http" => [
        "method" => "POST",
        "header" => "Authorization: Bearer $tok\r\nContent-Type: application/json\r\n",
        "content" => '{"uuid":"wyn2ah6rflg6ufwzpvzk436f","force":true,"instant_deploy":true}',
    ],
]);
echo file_get_contents("http://localhost:8080/api/v1/deploy", false, $ctx);
?>
