package com.errorfactory.agentping.data

import java.net.URI
import java.security.MessageDigest

/** Bind credentials and the offline replica to the full backend base URL, including its path. */
class BackendScope(baseUrl: String) {
    private val uri = URI(baseUrl).normalize()
    private val canonical =
        URI(
                uri.scheme.lowercase(),
                null,
                uri.host.lowercase(),
                if (uri.port == 443 && uri.scheme.equals("https", true)) -1 else uri.port,
                uri.path,
                null,
                null,
            )
            .toASCIIString()
    private val id =
        MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    // The old unscoped preferences/database are intentionally retained, never imported into prod.
    val preferencesName = "device_identity_$id"
    val databaseName = "agentping_$id.db"
}
