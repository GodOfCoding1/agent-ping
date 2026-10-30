package com.errorfactory.agentping.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.errorfactory.agentping.BuildConfig
import java.security.KeyStore
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** AES-GCM device credential, non-exportable Keystore key, no backup or logging. */
class SecureStore(context: Context, baseUrl: String = BuildConfig.BACKEND_BASE_URL) {
    private val prefs =
        context.getSharedPreferences(BackendScope(baseUrl).preferencesName, Context.MODE_PRIVATE)
    private val alias = "agentping.device.v1"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let {
            return it
        }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            .apply {
                init(
                    KeyGenParameterSpec.Builder(
                            alias,
                            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                        )
                        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                        .build(),
                )
            }
            .generateKey()
    }

    @get:Synchronized
    val installationId: String
        get() =
            prefs.getString("installation", null)
                ?: UUID.randomUUID().toString().also {
                    check(prefs.edit().putString("installation", it).commit())
                }

    var token: String?
        @Synchronized
        get() =
            prefs.getString("credential", null)?.let {
                val parts = it.split(":")
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(
                    Cipher.DECRYPT_MODE,
                    key(),
                    GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)),
                )
                String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), Charsets.UTF_8)
            }
        @Synchronized
        set(value) {
            if (value == null) {
                check(prefs.edit().remove("credential").commit())
                return
            }
            val cipher =
                Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
            val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            check(
                prefs
                    .edit()
                    .putString(
                        "credential",
                        Base64.encodeToString(cipher.iv, Base64.NO_WRAP) +
                            ":" +
                            Base64.encodeToString(encrypted, Base64.NO_WRAP),
                    )
                    .commit(),
            )
        }

    var deviceId: String
        get() = prefs.getString("device", "")!!
        set(value) {
            check(prefs.edit().putString("device", value).commit())
        }

    var fid: String?
        get() = prefs.getString("fid", null)
        set(value) {
            check(prefs.edit().putString("fid", value).commit())
        }
}
