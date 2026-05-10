package com.smartinventory.sms

import android.content.Context

class Config(ctx: Context) {
    private val prefs = ctx.applicationContext.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    var url: String
        get() = prefs.getString(KEY_URL, "") ?: ""
        set(v) { prefs.edit().putString(KEY_URL, v).apply() }

    var token: String
        get() = prefs.getString(KEY_TOKEN, "") ?: ""
        set(v) { prefs.edit().putString(KEY_TOKEN, v).apply() }

    /** comma/semicolon/newline-separated body keyword allow-list. empty = do not forward. */
    var keywordFilter: String
        get() = prefs.getString(KEY_KEYWORDS, prefs.getString(KEY_LEGACY_FILTER, "") ?: "") ?: ""
        set(v) { prefs.edit().putString(KEY_KEYWORDS, v).apply() }

    var enabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, true)
        set(v) { prefs.edit().putBoolean(KEY_ENABLED, v).apply() }

    companion object {
        private const val PREF = "sms_forwarder"
        private const val KEY_URL = "url"
        private const val KEY_TOKEN = "token"
        private const val KEY_KEYWORDS = "keywords"
        private const val KEY_LEGACY_FILTER = "filter"
        private const val KEY_ENABLED = "enabled"
    }
}

object AppLog {
    private const val PREF = "sms_forwarder_log"
    private const val KEY = "log"
    private const val MAX_LEN = 12_000

    fun append(ctx: Context, msg: String) {
        val prefs = ctx.applicationContext.getSharedPreferences(PREF, Context.MODE_PRIVATE)
        val ts = java.text.SimpleDateFormat("MM-dd HH:mm:ss", java.util.Locale.US)
            .format(java.util.Date())
        val existing = prefs.getString(KEY, "") ?: ""
        val combined = "[$ts] $msg\n$existing"
        prefs.edit().putString(KEY, combined.take(MAX_LEN)).apply()
    }

    fun read(ctx: Context): String =
        ctx.applicationContext.getSharedPreferences(PREF, Context.MODE_PRIVATE)
            .getString(KEY, "") ?: ""

    fun clear(ctx: Context) {
        ctx.applicationContext.getSharedPreferences(PREF, Context.MODE_PRIVATE)
            .edit().remove(KEY).apply()
    }
}
