package com.smartinventory.sms

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var cfg: Config

    private lateinit var urlEdit: EditText
    private lateinit var tokenEdit: EditText
    private lateinit var filterEdit: EditText
    private lateinit var enabledCheck: CheckBox
    private lateinit var statusText: TextView
    private lateinit var logText: TextView
    private lateinit var fetchedText: TextView

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        cfg = Config(this)

        urlEdit = findViewById(R.id.urlEdit)
        tokenEdit = findViewById(R.id.tokenEdit)
        filterEdit = findViewById(R.id.filterEdit)
        enabledCheck = findViewById(R.id.enabledCheck)
        statusText = findViewById(R.id.statusText)
        logText = findViewById(R.id.logText)
        fetchedText = findViewById(R.id.fetchedText)

        urlEdit.setText(cfg.url)
        tokenEdit.setText(cfg.token)
        filterEdit.setText(cfg.keywordFilter)
        enabledCheck.isChecked = cfg.enabled

        findViewById<Button>(R.id.saveBtn).setOnClickListener { saveConfig(showToast = true) }
        findViewById<Button>(R.id.permBtn).setOnClickListener { openNotificationAccess() }
        findViewById<Button>(R.id.testBtn).setOnClickListener { testConnection() }
        findViewById<Button>(R.id.fetchBtn).setOnClickListener { fetchRecent() }
        findViewById<Button>(R.id.clearLogBtn).setOnClickListener {
            AppLog.clear(this); refreshLog(); toast("로그 비움")
        }
        findViewById<Button>(R.id.batteryBtn).setOnClickListener { openBatteryOptimization() }

        enabledCheck.setOnCheckedChangeListener { _, isChecked ->
            cfg.enabled = isChecked
            refreshStatus()
        }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        refreshLog()
    }

    private fun saveConfig(showToast: Boolean = false) {
        cfg.url = urlEdit.text.toString().trim()
        cfg.token = tokenEdit.text.toString().trim()
        cfg.keywordFilter = filterEdit.text.toString().trim()
        cfg.enabled = enabledCheck.isChecked
        if (showToast) toast("저장됨")
        refreshStatus()
    }

    private fun openNotificationAccess() {
        saveConfig()
        try {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_SETTINGS))
        }
    }

    private fun openBatteryOptimization() {
        try {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_SETTINGS))
        }
    }

    private fun refreshStatus() {
        val notificationAccess = SmsNotificationListenerService.isEnabled(this)
        val urlSet = cfg.url.isNotBlank()
        val keywordSet = cfg.keywordFilter.isNotBlank()
        val on = cfg.enabled

        statusText.text = buildString {
            append("전송: ")
            append(if (on) "ON" else "OFF")
            append("  ·  URL: ")
            append(if (urlSet) "설정됨" else "미설정")
            append("\n알림 접근: ")
            append(if (notificationAccess) "OK" else "필요")
            append("  ·  키워드: ")
            append(if (keywordSet) "설정됨" else "미설정")
        }
    }

    private fun refreshLog() {
        logText.text = AppLog.read(this).ifEmpty { "(로그 없음)" }
    }

    private fun testConnection() {
        saveConfig()
        val base = cfg.url.trim().trimEnd('/')
        if (base.isEmpty()) {
            toast("URL을 먼저 입력하세요")
            return
        }
        val token = cfg.token.trim()
        if (token.isEmpty()) {
            toast("토큰을 먼저 입력하세요")
            return
        }
        statusText.text = "테스트 중..."
        thread {
            val nowMs = System.currentTimeMillis()
            val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
                timeZone = TimeZone.getDefault()
            }
            val deviceId = try {
                Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: ""
            } catch (_: Exception) {
                ""
            }
            val payload = JSONObject().apply {
                put("msg_key", "sms-test|$deviceId|$nowMs")
                put("sender", "SMS_FORWARDER_TEST")
                put("body", "[SMS 포워더 연결 테스트] 라즈베리 DB 저장 확인")
                put("received_at", fmt.format(Date(nowMs)))
                put("received_at_ms", nowMs)
                put("device_id", deviceId)
                put("raw", JSONObject().apply {
                    put("source", "android_connection_test")
                    put("app", packageName)
                })
            }
            val req = Request.Builder()
                .url("$base/sms-messages")
                .post(payload.toString().toRequestBody(JSON))
                .apply {
                    header("Authorization", "Bearer $token")
                    header("X-Api-Token", token)
                }
                .build()
            try {
                httpClient.newCall(req).execute().use { resp ->
                    val msg = if (resp.isSuccessful) "전송 테스트 OK (HTTP ${resp.code})"
                    else "응답 HTTP ${resp.code}"
                    AppLog.append(this, "테스트: $msg")
                    runOnUiThread {
                        toast(msg)
                        refreshStatus()
                        refreshLog()
                    }
                }
            } catch (e: Exception) {
                val em = e.message ?: e.javaClass.simpleName
                AppLog.append(this, "테스트 실패: $em")
                runOnUiThread {
                    toast("연결 실패: $em")
                    refreshStatus()
                    refreshLog()
                }
            }
        }
    }

    private fun fetchRecent() {
        saveConfig()
        val base = cfg.url.trim().trimEnd('/')
        if (base.isEmpty()) {
            toast("URL을 먼저 입력하세요")
            return
        }
        fetchedText.text = "불러오는 중..."
        thread {
            val req = Request.Builder()
                .url("$base/sms-messages?limit=20")
                .apply {
                    val t = cfg.token.trim()
                    if (t.isNotEmpty()) header("Authorization", "Bearer $t")
                }
                .build()
            try {
                httpClient.newCall(req).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    if (!resp.isSuccessful) {
                        runOnUiThread { fetchedText.text = "HTTP ${resp.code}\n$text" }
                        return@use
                    }
                    val arr = JSONObject(text).optJSONArray("items")
                    val sb = StringBuilder()
                    if (arr == null || arr.length() == 0) {
                        sb.append("(서버에 저장된 SMS 없음)")
                    } else {
                        sb.append("최근 ").append(arr.length()).append("건\n\n")
                        for (i in 0 until arr.length()) {
                            val o = arr.getJSONObject(i)
                            val ts = o.optString("received_at")
                            val sender = o.optString("sender")
                            val body = o.optString("body").replace("\n", " ")
                            sb.append(ts).append("  ")
                                .append(sender).append("\n")
                                .append(if (body.length > 80) body.take(80) + "…" else body)
                                .append("\n\n")
                        }
                    }
                    runOnUiThread { fetchedText.text = sb.toString() }
                }
            } catch (e: Exception) {
                val em = e.message ?: e.javaClass.simpleName
                runOnUiThread { fetchedText.text = "오류: $em" }
            }
        }
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

    @Suppress("unused")
    private fun urlEnc(s: String): String = URLEncoder.encode(s, "UTF-8")

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
