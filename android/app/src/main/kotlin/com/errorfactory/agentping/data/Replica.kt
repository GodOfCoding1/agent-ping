package com.errorfactory.agentping.data

import androidx.room.withTransaction
import com.google.gson.JsonObject

/** Cursor and all entity changes commit together. Pending local actions overlay server data. */
class Replica(private val db: LocalDatabase) {
    private val store = db.store()

    suspend fun apply(response: JsonObject) = db.withTransaction {
        val pendingIds =
            store
                .pending()
                .filter { it.kind == "action" }
                .map { it.json.json().str("occurrence_id") }
                .toSet()
        val overlays = store.list("occurrence").filter { it.id in pendingIds }
        if (response.bool("full"))
            listOf("reminder", "occurrence", "device").forEach { store.clearKind(it) }
        mapOf("reminders" to "reminder", "occurrences" to "occurrence", "devices" to "device")
            .forEach { (field, kind) ->
                response.array(field).forEach { element ->
                    val value = element.asJsonObject
                    val id = value.str("id")
                    val old = store.get(kind, id)?.json?.json()
                    val revision = if (kind == "occurrence") "version" else "revision"
                    if (old == null || value.int(revision) >= old.int(revision)) {
                        if (kind != "occurrence" || id !in pendingIds)
                            store.put(Entity(kind, id, value.toString()))
                    }
                }
            }
        overlays.forEach { store.put(it) }
        response
            .get("settings")
            ?.takeIf { it.isJsonObject }
            ?.let { store.put(Entity("settings", "current", it.toString())) }
        store.put(
            Entity(
                "meta",
                "sync",
                obj(
                        "cursor" to response.get("cursor"),
                        "server_time" to response.get("server_time"),
                        "horizon_end" to response.get("horizon_end"),
                        "last_sync" to java.time.Instant.now().toString(),
                    )
                    .toString(),
            ),
        )
    }
}
