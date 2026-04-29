package com.smartinventory.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * Static broadcast receiver — fires even when the app is closed.
 *
 * Multipart SMS arrives as multiple SmsMessage parts; we concatenate them
 * by `originatingAddress` so a single payload is queued per logical message.
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        val cfg = Config(context)
        if (!cfg.enabled) {
            AppLog.append(context, "수신했지만 전송 OFF — 무시")
            return
        }

        val msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (msgs.isEmpty()) return

        val isoFmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getDefault()
        }

        // Concatenate multipart parts per originator
        val byOriginator = msgs.groupBy { it.originatingAddress ?: "" }
        for ((sender, parts) in byOriginator) {
            val body = parts.joinToString("") { it.messageBody ?: "" }
            val tsMs = parts.firstOrNull()?.timestampMillis ?: System.currentTimeMillis()
            val iso = isoFmt.format(Date(tsMs))

            val data = Data.Builder()
                .putString("sender", sender)
                .putString("body", body)
                .putString("received_at", iso)
                .putLong("received_at_ms", tsMs)
                .build()

            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val req = OneTimeWorkRequestBuilder<ForwardWorker>()
                .setInputData(data)
                .setConstraints(constraints)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()

            WorkManager.getInstance(context).enqueue(req)
            AppLog.append(context, "수신: $sender · ${body.take(40).replace("\n", " ")}")
        }
    }
}
