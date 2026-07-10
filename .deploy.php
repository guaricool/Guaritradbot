<?php
// Manual deploy trigger via Coolify local API.
//
// Sprint 43 L9 fix: requires HMAC-SHA256 signature in `X-Signature`
// header. The shared secret is read from COOLIFY_DEPLOY_SECRET env
// var (NOT the COOLIFY_TOKEN — that one is the Coolify API auth
// credential, this one is a deploy-endpoint auth credential).
// Anyone calling this script (or hitting it over HTTP) must know
// the shared secret. The signature is computed over the
// timestamp + body to prevent replay attacks.
//
// Token: read from COOLIFY_TOKEN environment variable. NEVER hardcode
// the API token in this file (the repo is public on GitHub and
// tokens get scraped within hours of being committed).
//
// Usage:
//   1. Set COOLIFY_TOKEN and COOLIFY_DEPLOY_SECRET in your shell:
//        export COOLIFY_TOKEN=...
//        export COOLIFY_DEPLOY_SECRET=...
//   2. Run: `php .deploy.php`  (skips the HMAC check — CLI bypass)
//      or
//      `php .deploy.php --require-hmac`  (forces the HMAC check
//      even from CLI; useful for testing the auth path locally)
//
// HTTP usage (NOT recommended for production — use Coolify's
// built-in webhook on push to the main branch instead, which has
// its own auth model):
//   curl -X POST http://host/.deploy.php \
//        -H 'X-Signature: sha256=<hex>' \
//        -H 'X-Timestamp: <unix-seconds>' \
//        -d ''
//
//   The X-Signature is hex(hmac_sha256(secret, timestamp + ':' + body)).
//   The timestamp must be within 300 seconds of server time
//   (replay protection).
$tok = getenv("COOLIFY_TOKEN");
$secret = getenv("COOLIFY_DEPLOY_SECRET");
if ($tok === false || $tok === "") {
    fwrite(STDERR, "ERROR: COOLIFY_TOKEN env var is required. Set it in your shell.\n");
    exit(1);
}
if ($secret === false || $secret === "") {
    fwrite(STDERR, "ERROR: COOLIFY_DEPLOY_SECRET env var is required for HMAC auth. Set it in your shell.\n");
    exit(1);
}

// --- HMAC verification (Sprint 43 L9) ---
// Two entry modes:
//   (1) CLI: php .deploy.php  → skip HMAC (Carlos is on the box)
//   (2) HTTP: any request via web server → require HMAC
$require_hmac = in_array("--require-hmac", $argv, true)
    || (php_sapi_name() !== "cli");

if ($require_hmac) {
    $sig_header = $_SERVER["HTTP_X_SIGNATURE"] ?? "";
    $ts_header = $_SERVER["HTTP_X_TIMESTAMP"] ?? "";
    if ($sig_header === "" || $ts_header === "") {
        http_response_code(401);
        fwrite(STDERR, "ERROR: X-Signature and X-Timestamp headers required\n");
        exit(1);
    }
    // Replay protection: timestamp must be within 300s of now
    $now = time();
    $ts = (int)$ts_header;
    if (abs($now - $ts) > 300) {
        http_response_code(401);
        fwrite(STDERR, "ERROR: timestamp out of window (replay attack?): $ts vs $now\n");
        exit(1);
    }
    // Compute expected signature
    $body = file_get_contents("php://input") ?: "";
    $expected = hash_hmac("sha256", $ts . ":" . $body, $secret);
    // Strip any "sha256=" prefix the caller may include
    $provided = preg_replace('/^sha256=/', '', $sig_header);
    if (!hash_equals($expected, $provided)) {
        http_response_code(401);
        fwrite(STDERR, "ERROR: signature mismatch\n");
        exit(1);
    }
    fwrite(STDERR, "[Auth] HMAC signature OK, timestamp $ts within 300s window\n");
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
