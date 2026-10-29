package com.errorfactory.agentping.data

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.*

// Wire names deliberately match the strict shared REST/MCP models. JSON is retained
// in Room so future server fields survive a client round trip without data loss.
interface AgentPingApi {
    @POST("api/v1/devices/register") suspend fun pair(@Body body: JsonObject): JsonObject

    @PATCH("api/v1/devices/me") suspend fun updateSelf(@Body body: JsonObject): JsonObject

    @GET("api/v1/devices/me") suspend fun self(): JsonObject

    @POST("api/v1/devices/me/rotate-credential") suspend fun rotate(): JsonObject

    @GET("api/v1/sync") suspend fun sync(@Query("cursor") cursor: Long?): JsonObject

    @POST("api/v1/sync/actions") suspend fun actions(@Body body: JsonObject): JsonArray

    @POST("api/v1/devices/me/receipts") suspend fun receipts(@Body body: JsonObject): JsonObject

    @GET("api/v1/activity")
    suspend fun activity(
        @Query("cursor") cursor: String?,
        @Query("category") category: String? = null,
    ): JsonObject

    @GET("api/v1/agents") suspend fun agents(): JsonArray

    @POST("api/v1/reminders") suspend fun create(@Body body: JsonObject): JsonObject

    @GET("api/v1/reminders/{id}") suspend fun detail(@Path("id") id: String): JsonObject

    @PATCH("api/v1/reminders/{id}")
    suspend fun update(@Path("id") id: String, @Body body: JsonObject): JsonObject

    @POST("api/v1/reminders/{id}/cancel") suspend fun cancel(@Path("id") id: String): JsonObject

    @PATCH("api/v1/devices/{id}")
    suspend fun device(@Path("id") id: String, @Body body: JsonObject): JsonObject

    @DELETE("api/v1/devices/{id}") suspend fun removeDevice(@Path("id") id: String): JsonObject

    @PATCH("api/v1/settings") suspend fun settings(@Body body: JsonObject): JsonObject

    @GET("api/v1/diagnostics") suspend fun diagnostics(): JsonObject
}

fun api(baseUrl: String, secrets: SecureStore): AgentPingApi {
    val client =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor { chain ->
                val builder = chain.request().newBuilder()
                if (!chain.request().url.encodedPath.endsWith("/devices/register"))
                    secrets.token?.let { builder.header("Authorization", "Bearer $it") }
                chain.proceed(builder.build())
            }
            .build()
    return Retrofit.Builder()
        .baseUrl(baseUrl)
        .client(client)
        .addConverterFactory(GsonConverterFactory.create())
        .build()
        .create(AgentPingApi::class.java)
}

fun obj(vararg pairs: Pair<String, Any?>): JsonObject =
    JsonObject().apply {
        pairs.forEach { (key, value) ->
            when (value) {
                null -> Unit
                is com.google.gson.JsonElement -> add(key, value)
                is Boolean -> addProperty(key, value)
                is Number -> addProperty(key, value)
                else -> addProperty(key, value.toString())
            }
        }
    }

fun JsonObject.str(key: String, default: String = ""): String =
    get(key)?.takeUnless { it.isJsonNull }?.asString ?: default

fun JsonObject.int(key: String, default: Int = 0): Int =
    get(key)?.takeUnless { it.isJsonNull }?.asInt ?: default

fun JsonObject.bool(key: String, default: Boolean = false): Boolean =
    get(key)?.takeUnless { it.isJsonNull }?.asBoolean ?: default

fun JsonObject.child(key: String): JsonObject =
    get(key)?.takeIf { it.isJsonObject }?.asJsonObject ?: JsonObject()

fun JsonObject.array(key: String): JsonArray =
    get(key)?.takeIf { it.isJsonArray }?.asJsonArray ?: JsonArray()

fun String.json(): JsonObject = com.google.gson.JsonParser.parseString(this).asJsonObject
