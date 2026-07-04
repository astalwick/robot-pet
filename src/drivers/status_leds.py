"""Voice status LEDs wired directly to GPIO pins.

Temporary wiring — likely replaced by charlieplexing or a shift register
later. To remove: delete this file and the two `self.leds` lines in
robot_voice.py.

- GPIO 17: voice subsystem on (wake word armed / dashboard "Voice On")
- GPIO 22: sending audio to STT or waiting on a transcript
- GPIO 27: LLM generating a response or running a tool call
"""

from lib.log import setup_logging

VOICE_ON_PIN = 17
STT_ACTIVE_PIN = 22
LLM_ACTIVE_PIN = 27

log = setup_logging("status-leds")


class StatusLeds:
    def __init__(self) -> None:
        try:
            from gpiozero import LED

            self._leds = (LED(VOICE_ON_PIN), LED(STT_ACTIVE_PIN), LED(LLM_ACTIVE_PIN))
        except Exception as exc:
            log.warning("status LEDs unavailable: %s", exc)
            self._leds = None

    def update(self, voice_on: bool, stt_active: bool, llm_active: bool) -> None:
        if self._leds is None:
            return
        for led, lit in zip(self._leds, (voice_on, stt_active, llm_active)):
            led.value = lit
