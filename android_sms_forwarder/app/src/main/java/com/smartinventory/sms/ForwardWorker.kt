package com.smartinventory.sms

import android.content.Context
import android.os.Build
import android.provider.Settings
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONException
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * SMS 1건을 라즈베리 서버에 POST.
 *
 * 입력 Data:
 *   - sender:        String
 *   - body:          String
 *   - received_at:   String (ISO-8601, local TZ)
 *   - received_at_ms: Long (선택 — 없으면 현재 시각)
 *
 * 실패 시 WorkManager 가 지수 백오프로 자동 재시도. 인터넷이 돌아오면 큐에 쌓인
 * 메시지가 순차적으로 전송된다.
 */
class ForwardWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val cfg = Config(ctx)
        val urlBase = cfg.url.trim().trimEnd('/')
        if (urlBase.isEmpty()) {
            AppLog.append(ctx, "전송 실패: 서버 URL 미설정")
            return Result.failure()
        }

        val sender = inputData.getString("sender").orEmpty()
        val body = inputData.getString("body").orEmpty()
        val receivedAt = inputData.getString("received_at").orEmpty().ifBlank {
            val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
                timeZone = TimeZone.getDefault()
            }
            fmt.format(Date())
        }
        val receivedAtMs = inputData.getLong("received_at_ms", System.currentTimeMillis())

        val deviceId = try {
            Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID) ?: ""
        } catch (_: Exception) {
            ""
        }

        val msgKey = inputData.getString("msg_key").orEmpty().ifBlank {
            "sms|$sender|$receivedAtMs"
        }
        val rawText = inputData.getString("raw").orEmpty()

        val payload = JSONObject().apply {
            put("msg_key", msgKey)
            put("sender", sender)
            put("body", body)
            put("received_at", receivedAt)
            put("received_at_ms", receivedAtMs)
            put("device_id", deviceId)
            put("raw", JSONObject().apply {
                put("device_model", Build.MODEL ?: "")
                put("android_sdk", Build.VERSION.SDK_INT)
                if (rawText.isNotBlank()) {
                    try {
                        put("sms", JSONObject(rawText))
                    } catch (_: JSONException) {
                        put("sms", rawText)
                    }
                }
            })
        }

        val req = Request.Builder()
            .url("$urlBase/sms-messages")
            .post(payload.toString().toRequestBody(JSON))
            .apply {
                val token = cfg.token.trim()
                if (token.isNotEmpty()) {
                    header("Authorization", "Bearer $token")
                    header("X-Api-Token", token)
                }
            }
            .build()

        return try {
            client.newCall(req).execute().use { resp ->
                if (resp.isSuccessful) {
                    AppLog.append(ctx, "전송 OK: $sender · ${body.take(40).replace("\n", " ")}")
                    Result.success()
                } else {
                    val code = resp.code
                    AppLog.append(ctx, "전송 실패(HTTP $code) — 재시도 예약: $sender")
                    if (code in 400..499 && code != 408 && code != 429) Result.failure()
                    else Result.retry()
                }
            }
        } catch (e: Exception) {
            AppLog.append(ctx, "전송 오류: ${e.message ?: e.javaClass.simpleName} — 재시도")
            Result.retry()
        }
    }

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
