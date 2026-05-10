package com.smartinventory.sms

import android.app.Notification
import android.content.Context
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

class SmsNotificationListenerService : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val ctx = applicationContext
        val cfg = Config(ctx)
        if (!cfg.enabled) return

        val body = extractBody(sbn.notification.extras).trim()
        if (body.isEmpty()) return

        val sender = extractSender(sbn.notification.extras, sbn.packageName)
        val fullText = "$sender\n$body"
        val keywords = cfg.keywordFilter
            .split(',', ';', '\n')
            .map { it.trim() }
            .filter { it.isNotEmpty() }
        if (keywords.isEmpty()) {
            AppLog.append(ctx, "키워드 미설정 — 알림 무시")
            return
        }
        val matchedKeyword = keywords.firstOrNull { fullText.contains(it, ignoreCase = true) } ?: run {
            AppLog.append(ctx, "키워드 제외 알림: ${sbn.packageName}")
            return
        }

        val tsMs = if (sbn.postTime > 0) sbn.postTime else System.currentTimeMillis()
        val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getDefault()
        }.format(Date(tsMs))
        val deviceId = try {
            Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: ""
        } catch (_: Exception) {
            ""
        }

        val raw = JSONObject().apply {
            put("source", "notification_listener")
            put("matched_keyword", matchedKeyword)
            put("package_name", sbn.packageName)
            put("notification_key", sbn.key ?: "")
            put("title", safeString(sbn.notification.extras, Notification.EXTRA_TITLE))
            put("text", safeString(sbn.notification.extras, Notification.EXTRA_TEXT))
            put("big_text", safeString(sbn.notification.extras, Notification.EXTRA_BIG_TEXT))
        }

        val data = Data.Builder()
            .putString("sender", sender)
            .putString("body", body)
            .putString("received_at", iso)
            .putLong("received_at_ms", tsMs)
            .putString("raw", raw.toString())
            .putString("msg_key", "sms-notification|$deviceId|${sbn.key ?: "$sender|$tsMs"}")
            .build()

        val req = OneTimeWorkRequestBuilder<ForwardWorker>()
            .setInputData(data)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()

        WorkManager.getInstance(ctx).enqueue(req)
        AppLog.append(ctx, "알림 큐 저장: $sender · $matchedKeyword · ${body.take(40).replace("\n", " ")}")
    }

    private fun extractSender(extras: Bundle, fallback: String): String {
        return safeString(extras, Notification.EXTRA_TITLE)
            .ifBlank { safeString(extras, Notification.EXTRA_SUB_TEXT) }
            .ifBlank { fallback }
    }

    private fun extractBody(extras: Bundle): String {
        val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
        if (!lines.isNullOrEmpty()) {
            return lines.joinToString("\n") { it?.toString().orEmpty() }.trim()
        }
        return safeString(extras, Notification.EXTRA_BIG_TEXT)
            .ifBlank { safeString(extras, Notification.EXTRA_TEXT) }
    }

    private fun safeString(extras: Bundle, key: String): String {
        return extras.getCharSequence(key)?.toString().orEmpty()
    }

    companion object {
        fun isEnabled(context: Context): Boolean {
            val enabled = Settings.Secure.getString(
                context.contentResolver,
                "enabled_notification_listeners",
            ).orEmpty()
            return enabled.contains(context.packageName, ignoreCase = true)
        }
    }
}
