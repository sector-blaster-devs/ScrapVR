package studio.roundup.scrapvr;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Opening the app starts the sender; switch to Moonlight and it keeps running. */
public class MainActivity extends Activity {
    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView status;
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            status.setText(PoseService.status);
            ui.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 48, 48, 48);

        Button start = new Button(this);
        start.setText("Start sending gyro");
        // Anonymous listeners, not lambdas: android.jar has no LambdaMetafactory for javac.
        start.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                startForegroundService(new Intent(MainActivity.this, PoseService.class));
            }
        });
        Button stop = new Button(this);
        stop.setText("Stop");
        stop.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                stopService(new Intent(MainActivity.this, PoseService.class));
            }
        });
        status = new TextView(this);
        status.setTextSize(18);

        root.addView(start);
        root.addView(stop);
        root.addView(status);
        setContentView(root);
        startForegroundService(new Intent(this, PoseService.class));
        // For the headset calibration only (CameraServer); the gyro works without it.
        if (checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {android.Manifest.permission.CAMERA}, 1);
        }
    }

    @Override protected void onResume() { super.onResume(); ui.post(tick); }
    @Override protected void onPause() { super.onPause(); ui.removeCallbacks(tick); }
}
