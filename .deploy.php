<?php
$tok = "9|yqNYDjMmh0t48pQYgbWOXVpDx4iuyPmG8ElJDL7Zab3c1f67";
$ctx = stream_context_create([
    "http" => [
        "method" => "POST",
        "header" => "Authorization: Bearer $tok\r\nContent-Type: application/json\r\n",
        "content" => '{"uuid":"wyn2ah6rflg6ufwzpvzk436f","force":true,"instant_deploy":true}',
    ],
]);
echo file_get_contents("http://localhost:8080/api/v1/deploy", false, $ctx);
?>