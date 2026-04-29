package com.smartinventory.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * BOOT_COMPLETED 후 정적 SmsReceiver 가 자동 등록되는 것은 OS 가 처리하지만,
 * 부팅 직후 사용자에게 앱이 실행 중인지 확인할 수 있도록 로그만 남긴다.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            AppLog.append(context, "부팅 완료 — SMS 수신 대기 중")
        }
    }
}
