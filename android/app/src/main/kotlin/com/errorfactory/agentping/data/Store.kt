package com.errorfactory.agentping.data

import android.content.Context
import androidx.room.*
import kotlinx.coroutines.flow.Flow

@androidx.room.Entity(tableName = "entities", primaryKeys = ["kind", "id"])
data class Entity(val kind: String, val id: String, val json: String)

@androidx.room.Entity(tableName = "pending")
data class Pending(
    @PrimaryKey val id: String,
    val kind: String,
    val json: String,
    val created: Long,
)

@androidx.room.Entity(tableName = "events")
data class Event(
    @PrimaryKey val id: String,
    val occurrenceId: String,
    val version: Int,
    val phase: String,
    val at: Long,
)

@Dao
interface Store {
    @Query("DELETE FROM entities") suspend fun clearEntities()

    @Query("DELETE FROM pending") suspend fun clearPending()

    @Query("DELETE FROM events") suspend fun clearEvents()

    @Query("SELECT * FROM entities") fun observe(): Flow<List<Entity>>

    @Query("SELECT * FROM entities WHERE kind=:kind") suspend fun list(kind: String): List<Entity>

    @Query("SELECT * FROM entities WHERE kind=:kind AND id=:id")
    suspend fun get(kind: String, id: String): Entity?

    @Upsert suspend fun put(entity: Entity)

    @Query("DELETE FROM entities WHERE kind=:kind AND id=:id")
    suspend fun delete(kind: String, id: String)

    @Query("DELETE FROM entities WHERE kind=:kind") suspend fun clearKind(kind: String)

    @Query("SELECT * FROM pending ORDER BY created, id") suspend fun pending(): List<Pending>

    @Query("SELECT * FROM pending ORDER BY created, id") fun observePending(): Flow<List<Pending>>

    @Upsert suspend fun queue(value: Pending)

    @Query("DELETE FROM pending WHERE id=:id") suspend fun dequeue(id: String)

    @Insert(onConflict = OnConflictStrategy.IGNORE) suspend fun claim(event: Event): Long

    @Query("UPDATE events SET phase=:phase WHERE id=:id")
    suspend fun phase(id: String, phase: String)

    @Query(
        "SELECT * FROM events WHERE occurrenceId=:id AND version=:version AND phase='displayed' LIMIT 1",
    )
    suspend fun displayed(id: String, version: Int): Event?

    @Query("SELECT * FROM events WHERE id=:id") suspend fun event(id: String): Event?

    @Query("SELECT * FROM events") suspend fun events(): List<Event>
}

@Database(
    entities = [Entity::class, Pending::class, Event::class],
    version = 1,
    exportSchema = true,
)
abstract class LocalDatabase : RoomDatabase() {
    abstract fun store(): Store

    companion object {
        fun open(context: Context) =
            Room.databaseBuilder(context, LocalDatabase::class.java, "agentping.db").build()
    }
}
