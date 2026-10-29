package com.errorfactory.agentping

import android.Manifest
import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject

object Alerts {
    val modes = listOf("normal", "persistent", "alarm", "critical")

    fun channels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        modes.forEach { mode ->
            manager.createNotificationChannel(
                NotificationChannel(
                        mode,
                        when (mode) {
                            "normal" -> "Normal notifications"
                            "persistent" -> "Persistent reminders"
                            "alarm" -> "Alarms"
                            else -> "Critical alerts"
                        },
                        if (mode == "normal") NotificationManager.IMPORTANCE_DEFAULT
                        else NotificationManager.IMPORTANCE_HIGH,
                    )
                    .apply {
                        description =
                            "AgentPing $mode alerts • sound and vibration are controlled here"
                        enableVibration(true)
                        if (mode == "alarm")
                            setSound(
                                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                                AudioAttributes.Builder()
                                    .setUsage(AudioAttributes.USAGE_ALARM)
                                    .build(),
                            )
                    },
            )
        }
    }

    fun permitted(context: Context) =
        NotificationManagerCompat.from(context).areNotificationsEnabled() &&
            (Build.VERSION.SDK_INT < 33 ||
                ContextCompat.checkSelfPermission(
                    context,
                    Manifest.permission.POST_NOTIFICATIONS,
                ) == PackageManager.PERMISSION_GRANTED)

    fun exact(context: Context) =
        Build.VERSION.SDK_INT < 31 ||
            context.getSystemService(AlarmManager::class.java).canScheduleExactAlarms()

    fun fullScreen(context: Context) =
        Build.VERSION.SDK_INT < 34 ||
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()

    fun capabilities(context: Context): JsonObject {
        val manager = context.getSystemService(NotificationManager::class.java)
        return obj(
            "sdk_int" to Build.VERSION.SDK_INT,
            "notifications_permitted" to permitted(context),
            "exact_alarms_permitted" to exact(context),
            "full_screen_intent_permitted" to fullScreen(context),
            "dnd_access_granted" to manager.isNotificationPolicyAccessGranted,
            "ignoring_battery_optimizations" to
                context
                    .getSystemService(PowerManager::class.java)
                    .isIgnoringBatteryOptimizations(context.packageName),
            "background_restricted" to
                (Build.VERSION.SDK_INT >= 28 &&
                    context.getSystemService(ActivityManager::class.java).isBackgroundRestricted),
            "channels" to
                JsonObject().apply {
                    modes.forEach { mode ->
                        val channel = manager.getNotificationChannel(mode)
                        add(
                            mode,
                            obj(
                                "enabled" to (channel != null && channel.importance > 0),
                                "importance" to (channel?.importance ?: 0),
                                "bypass_dnd" to (channel?.canBypassDnd() ?: false),
                            ),
                        )
                    }
                },
        )
    }

    fun open(context: Context, id: String = ""): PendingIntent =
        PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java)
                .setData(android.net.Uri.parse("agentping://occurrence/$id")),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    fun action(context: Context, id: String, kind: String, minutes: Int = 10): PendingIntent =
        PendingIntent.getBroadcast(
            context,
            0,
            Intent(context, ActionReceiver::class.java)
                .setData(android.net.Uri.parse("agentping://action/$id/$kind/$minutes"))
                .putExtra("id", id)
                .putExtra("action", kind)
                .putExtra("minutes", minutes),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    fun notification(context: Context, id: String, payload: JsonObject): Notification {
        val mode = payload.str("mode", "normal").takeIf { it in modes } ?: "normal"
        val builder =
            NotificationCompat.Builder(context, mode)
                .addExtras(
                    android.os.Bundle().apply {
                        putString("agentping_event", payload.str("display_key"))
                        putString("agentping_occurrence", id)
                    },
                )
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(payload.str("title", "AgentPing"))
                .setContentText(payload.str("body", "Open AgentPing for details"))
                .setStyle(NotificationCompat.BigTextStyle().bigText(payload.str("body")))
                .setContentIntent(open(context, id))
                .setAutoCancel(mode == "normal")
                .setCategory(
                    if (mode == "alarm") NotificationCompat.CATEGORY_ALARM
                    else NotificationCompat.CATEGORY_REMINDER,
                )
                .setVisibility(
                    when (payload.str("visibility")) {
                        "public" -> NotificationCompat.VISIBILITY_PUBLIC
                        "secret" -> NotificationCompat.VISIBILITY_SECRET
                        else -> NotificationCompat.VISIBILITY_PRIVATE
                    },
                )
                .setOnlyAlertOnce(payload.str("escalation_step", "0") == "0")
                .setSilent(payload.str("silent") == "1")
                .setOngoing(mode == "persistent" || mode == "alarm")
                .setTimeoutAfter(
                    runCatching {
                            java.time.Instant.parse(payload.str("expires_at")).toEpochMilli() -
                                System.currentTimeMillis()
                        }
                        .getOrDefault(3600000)
                        .coerceAtLeast(1),
                )
                .setDeleteIntent(action(context, id, "skip"))
                .extend(WatchBridge.extension(id))
        val buttons =
            payload.get("actions")?.let {
                if (it.isJsonArray) it.asJsonArray
                else
                    runCatching { com.google.gson.JsonParser.parseString(it.asString).asJsonArray }
                        .getOrNull()
            }
        if (buttons != null && buttons.size() > 0)
            buttons.take(3).forEach { element ->
                val button = element.asJsonObject
                val pending =
                    if (button.str("kind") == "open") open(context, id)
                    else
                        action(
                            context,
                            id,
                            button.str("kind"),
                            button.int("snooze_minutes", payload.int("snooze_minutes", 10)),
                        )
                builder.addAction(0, button.str("label"), pending)
            }
        else
            builder
                .addAction(0, "Acknowledge", action(context, id, "acknowledge"))
                .addAction(
                    0,
                    "Snooze",
                    action(context, id, "snooze", payload.int("snooze_minutes", 10)),
                )
                .addAction(0, "Dismiss", action(context, id, "skip"))
        if (mode == "alarm" && fullScreen(context))
            builder.setFullScreenIntent(
                PendingIntent.getActivity(
                    context,
                    0,
                    Intent(context, AlarmActivity::class.java)
                        .setData(android.net.Uri.parse("agentping://ring/$id"))
                        .putExtra("id", id),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
                true,
            )
        return builder.build()
    }

    fun show(context: Context, id: String, payload: JsonObject): Boolean {
        val mode = payload.str("mode", "normal").takeIf { it in modes } ?: "normal"
        if (
            !permitted(context) ||
                context
                    .getSystemService(NotificationManager::class.java)
                    .getNotificationChannel(mode)
                    ?.importance == 0
        )
            return false
        val manager = context.getSystemService(NotificationManager::class.java)
        if (mode == "alarm") manager.notify(42, notification(context, id, payload))
        else manager.notify(id, 0, notification(context, id, payload))
        return true
    }

    fun cancel(context: Context, id: String) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.cancel(id, 0)
        if (
            manager.activeNotifications.any {
                it.id == 42 && it.notification.extras.getString("agentping_occurrence") == id
            }
        )
            manager.cancel(42)
    }
}
