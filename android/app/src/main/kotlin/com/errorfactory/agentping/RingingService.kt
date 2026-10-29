package com.errorfactory.agentping

import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.launch

/** One bounded active ringtone. Schedules and sync never depend on this service. */
class RingingService : Service() {
    private var player: MediaPlayer? = null
    private var wake: PowerManager.WakeLock? = null
    private val handler = Handler(Looper.getMainLooper())
    private val timeout = Runnable { stopSelf() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val id = intent?.getStringExtra("id") ?: return START_NOT_STICKY.also { stopSelf() }
        val payload =
            intent.getStringExtra("payload")?.json() ?: return START_NOT_STICKY.also { stopSelf() }
        try {
            val type =
                if (android.os.Build.VERSION.SDK_INT >= 34 && Alerts.exact(this))
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SYSTEM_EXEMPTED
                else android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            androidx.core.app.ServiceCompat.startForeground(
                this,
                42,
                Alerts.notification(this, id, payload),
                type,
            )
        } catch (_: RuntimeException) {
            app.scope.launch {
                app.repository.receipt(
                    id,
                    payload.int("occurrence_version", 1),
                    "failed",
                    "Android blocked foreground ringing; notification remains visible",
                )
            }
            stopSelf()
            return START_NOT_STICKY
        }
        active = id
        player?.release()
        wake?.let { if (it.isHeld) it.release() }
        wake =
            getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AgentPing:ring")
                .apply { acquire(125000) }
        try {
            val sound =
                getSystemService(android.app.NotificationManager::class.java)
                    .getNotificationChannel("alarm")
                    ?.sound
            if (sound != null)
                player =
                    MediaPlayer().apply {
                        setAudioAttributes(
                            AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_ALARM)
                                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                                .build(),
                        )
                        setDataSource(this@RingingService, sound)
                        isLooping = true
                        prepare()
                        start()
                    }
        } catch (_: Exception) {
            app.scope.launch {
                app.repository.receipt(
                    id,
                    payload.int("occurrence_version", 1),
                    "failed",
                    "Ringtone unavailable; notification remains visible",
                )
            }
        }
        handler.removeCallbacks(timeout)
        handler.postDelayed(
            timeout,
            minOf(
                120000L,
                runCatching {
                        java.time.Instant.parse(payload.str("expires_at")).toEpochMilli() -
                            System.currentTimeMillis()
                    }
                    .getOrDefault(120000L)
                    .coerceAtLeast(1),
            ),
        )
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacks(timeout)
        player?.release()
        player = null
        wake?.let { if (it.isHeld) it.release() }
        wake = null
        active = null
        super.onDestroy()
    }

    companion object {
        @Volatile var active: String? = null

        fun stop(context: Context, id: String) {
            if (active == id) context.stopService(Intent(context, RingingService::class.java))
        }
    }
}
