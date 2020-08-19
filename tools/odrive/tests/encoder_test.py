
import test_runner

import time
from math import pi
import os

from fibre.utils import Logger
from odrive.enums import *
from test_runner import *


class TestEncoderBase():
    """
    Base class for encoder tests.
    TODO: incremental encoder doesn't use this yet.

    All encoder tests expect the encoder to run at a constant velocity.
    This can be achieved by generating an encoder signal with a Teensy.

    During 5 seconds, several variables are recorded and then compared against
    the expected waveform. This is either a straight line, a sawtooth function
    or a constant.
    """

    def run_generic_encoder_test(self, encoder, true_cpr, true_rps, noise=1):
        encoder.config.cpr = true_cpr
        true_cps = true_cpr * true_rps

        encoder.set_linear_count(0) # prevent numerical errors
        data = record_log(lambda: [
            encoder.shadow_count,
            encoder.count_in_cpr,
            encoder.phase,
            encoder.pos_estimate_counts,
            encoder.pos_cpr_counts,
            encoder.vel_estimate_counts,
        ], duration=5.0)

        short_period = (abs(1 / true_rps) < 5.0)
        reverse = (true_rps < 0)

        # encoder.shadow_count
        slope, offset, fitted_curve = fit_line(data[:,(0,1)])
        test_assert_eq(slope, true_cps, accuracy=0.005)
        test_curve_fit(data[:,(0,1)], fitted_curve, max_mean_err = true_cpr * 0.02, inlier_range = true_cpr * 0.02, max_outliers = len(data[:,0]) * 0.02)

        # encoder.count_in_cpr
        slope, offset, fitted_curve = fit_sawtooth(data[:,(0,2)], true_cpr if reverse else 0, 0 if reverse else true_cpr)
        test_assert_eq(slope, true_cps, accuracy=0.005)
        test_curve_fit(data[:,(0,2)], fitted_curve, max_mean_err = true_cpr * 0.02, inlier_range = true_cpr * 0.02, max_outliers = len(data[:,0]) * 0.02)

        # encoder.phase
        slope, offset, fitted_curve = fit_sawtooth(data[:,(0,3)], pi if reverse else -pi, -pi if reverse else pi, sigma=5)
        test_assert_eq(slope / 7, 2*pi*true_rps, accuracy=0.05)
        test_curve_fit(data[:,(0,3)], fitted_curve, max_mean_err = true_cpr * 0.02, inlier_range = true_cpr * 0.02, max_outliers = len(data[:,0]) * 0.02)

        # encoder.pos_estimate
        slope, offset, fitted_curve = fit_line(data[:,(0,4)])
        test_assert_eq(slope, true_cps, accuracy=0.005)
        test_curve_fit(data[:,(0,4)], fitted_curve, max_mean_err = true_cpr * 0.02, inlier_range = true_cpr * 0.03, max_outliers = len(data[:,0]) * 0.03)
        
        # encoder.pos_cpr
        slope, offset, fitted_curve = fit_sawtooth(data[:,(0,5)], true_cpr if reverse else 0, 0 if reverse else true_cpr)
        test_assert_eq(slope, true_cps, accuracy=0.005)
        test_curve_fit(data[:,(0,5)], fitted_curve, max_mean_err = true_cpr * 0.05, inlier_range = true_cpr * 0.05, max_outliers = len(data[:,0]) * 0.02)

        # encoder.vel_estimate
        slope, offset, fitted_curve = fit_line(data[:,(0,6)])
        test_assert_eq(slope, 0.0, range = true_cpr * abs(true_rps) * 0.01)
        test_assert_eq(offset, true_cpr * true_rps, accuracy = 0.02)
        test_curve_fit(data[:,(0,6)], fitted_curve, max_mean_err = true_cpr * 0.05 * noise, inlier_range = true_cpr * 0.05 * noise, max_outliers = len(data[:,0]) * 0.05 * noise)



teensy_incremental_encoder_emulation_code = """
void setup() {
  pinMode({enc_a}, OUTPUT);
  pinMode({enc_b}, OUTPUT);
}

int cpr = 8192;
int rpm = 30;

// the loop routine runs over and over again forever:
void loop() {
  int microseconds_per_count = (1000000 * 60 / cpr / rpm);

  for (;;) {
    digitalWrite({enc_a}, HIGH);
    delayMicroseconds(microseconds_per_count);
    digitalWrite({enc_b}, HIGH);
    delayMicroseconds(microseconds_per_count);
    digitalWrite({enc_a}, LOW);
    delayMicroseconds(microseconds_per_count);
    digitalWrite({enc_b}, LOW);
    delayMicroseconds(microseconds_per_count);
  }
}
"""

class TestIncrementalEncoder(TestEncoderBase):

    def get_test_cases(self, testrig: TestRig):
        for odrive in testrig.get_components(ODriveComponent):
            for encoder in odrive.encoders:
                # Find the Teensy that is connected to the encoder pins and the corresponding Teensy GPIOs

                gpio_conns = [
                    testrig.get_directly_connected_components(encoder.a),
                    testrig.get_directly_connected_components(encoder.b),
                ]

                valid_combinations = [
                    (combination[0].parent,) + tuple(combination)
                    for combination in itertools.product(*gpio_conns)
                    if ((len(set(c.parent for c in combination)) == 1) and isinstance(combination[0].parent, TeensyComponent))
                ]

                yield (encoder, valid_combinations)


    def run_test(self, enc: ODriveEncoderComponent, teensy: TeensyComponent, teensy_gpio_a: TeensyGpio, teensy_gpio_b: TeensyGpio, logger: Logger):
        true_cps = 8192*0.5 # counts per second generated by the virtual encoder
        
        code = teensy_incremental_encoder_emulation_code.replace("{enc_a}", str(teensy_gpio_a.num)).replace("{enc_b}", str(teensy_gpio_b.num))
        teensy.compile_and_program(code)

        if enc.handle.config.mode != ENCODER_MODE_INCREMENTAL:
            enc.handle.config.mode = ENCODER_MODE_INCREMENTAL
            enc.parent.save_config_and_reboot()
        else:
            time.sleep(1.0) # wait for PLLs to stabilize

        enc.handle.config.bandwidth = 1000

        logger.debug("testing with 8192 CPR...")
        self.run_generic_encoder_test(enc.handle, 8192, true_cps / 8192)
        logger.debug("testing with 65536 CPR...")
        self.run_generic_encoder_test(enc.handle, 65536, true_cps / 65536)
        enc.handle.config.cpr = 8192



teensy_sin_cos_encoder_emulation_code = """
void setup() {
  analogWriteResolution(10);
  int freq = 150000000/1024; // ~146.5kHz PWM frequency
  analogWriteFrequency({enc_sin}, freq);
  analogWriteFrequency({enc_cos}, freq);
}

float rps = 1.0f;
float pos = 0;

void loop() {
  pos += 0.001f * rps;
  if (pos > 1.0f)
    pos -= 1.0f;
  analogWrite({enc_sin}, (int)(512.0f + 512.0f * sin(2.0f * M_PI * pos)));
  analogWrite({enc_cos}, (int)(512.0f + 512.0f * cos(2.0f * M_PI * pos)));
  delay(1);
}
"""

class TestSinCosEncoder(TestEncoderBase):
    def get_test_cases(self, testrig: TestRig):
        for odrive in testrig.get_components(ODriveComponent):
            gpio_conns = [
                testrig.get_directly_connected_components(odrive.gpio3),
                testrig.get_directly_connected_components(odrive.gpio4),
            ]

            valid_combinations = [
                (combination[0].parent,) + tuple(combination)
                for combination in itertools.product(*gpio_conns)
                if ((len(set(c.parent for c in combination)) == 1) and isinstance(combination[0].parent, TeensyComponent))
            ]

            yield (odrive.encoders[0], valid_combinations)


    def run_test(self, enc: ODriveEncoderComponent, teensy: TeensyComponent, teensy_gpio_sin: TeensyGpio, teensy_gpio_cos: TeensyGpio, logger: Logger):
        code = teensy_sin_cos_encoder_emulation_code.replace("{enc_sin}", str(teensy_gpio_sin.num)).replace("{enc_cos}", str(teensy_gpio_cos.num))
        teensy.compile_and_program(code)

        if enc.handle.config.mode != ENCODER_MODE_SINCOS:
            enc.parent.disable_mappings()
            enc.parent.handle.config.gpio3_mode = GPIO_MODE_ANALOG_IN
            enc.parent.handle.config.gpio4_mode = GPIO_MODE_ANALOG_IN
            enc.handle.config.mode = ENCODER_MODE_SINCOS
            enc.parent.save_config_and_reboot()
        else:
            time.sleep(1.0) # wait for PLLs to stabilize

        enc.handle.config.bandwidth = 100

        self.run_generic_encoder_test(enc.handle, 6283, 1.0, 2.0)



teensy_hall_effect_encoder_emulation_code = """
void setup() {
  pinMode({hall_a}, OUTPUT);
  pinMode({hall_b}, OUTPUT);
  pinMode({hall_c}, OUTPUT);
  digitalWrite({hall_a}, HIGH);
}

int cpr = 90; // 15 pole-pairs. Value suggested in hoverboard.md
float rps = 1.0f;
int us_per_count = (1000000.0f / cpr / rps);

void loop() {
  digitalWrite({hall_b}, HIGH);
  delayMicroseconds(us_per_count);
  digitalWrite({hall_a}, LOW);
  delayMicroseconds(us_per_count);
  digitalWrite({hall_c}, HIGH);
  delayMicroseconds(us_per_count);
  digitalWrite({hall_b}, LOW);
  delayMicroseconds(us_per_count);
  digitalWrite({hall_a}, HIGH);
  delayMicroseconds(us_per_count);
  digitalWrite({hall_c}, LOW);
  delayMicroseconds(us_per_count);
}
"""

class TestHallEffectEncoder(TestEncoderBase):

    def get_test_cases(self, testrig: TestRig):
        for odrive in testrig.get_components(ODriveComponent):
            for encoder in odrive.encoders:
                # Find the Teensy that is connected to the encoder pins and the corresponding Teensy GPIOs

                gpio_conns = [
                    testrig.get_directly_connected_components(encoder.a),
                    testrig.get_directly_connected_components(encoder.b),
                    testrig.get_directly_connected_components(encoder.z),
                ]

                valid_combinations = [
                    (combination[0].parent,) + tuple(combination)
                    for combination in itertools.product(*gpio_conns)
                    if ((len(set(c.parent for c in combination)) == 1) and isinstance(combination[0].parent, TeensyComponent))
                ]

                yield (encoder, valid_combinations)


    def run_test(self, enc: ODriveEncoderComponent, teensy: TeensyComponent, teensy_gpio_a: TeensyGpio, teensy_gpio_b: TeensyGpio, teensy_gpio_c: TeensyGpio, logger: Logger):
        true_cpr = 90
        true_rps = 1.0
        
        code = teensy_hall_effect_encoder_emulation_code.replace("{hall_a}", str(teensy_gpio_a.num)).replace("{hall_b}", str(teensy_gpio_b.num)).replace("{hall_c}", str(teensy_gpio_c.num))
        teensy.compile_and_program(code)

        if enc.handle.config.mode != ENCODER_MODE_HALL:
            if enc.num:
              enc.parent.handle.config.gpio9_mode = GPIO_MODE_DIGITAL
              enc.parent.handle.config.gpio10_mode = GPIO_MODE_DIGITAL
              enc.parent.handle.config.gpio11_mode = GPIO_MODE_DIGITAL
            else:
              enc.parent.handle.config.gpio12_mode = GPIO_MODE_DIGITAL
              enc.parent.handle.config.gpio13_mode = GPIO_MODE_DIGITAL
              enc.parent.handle.config.gpio14_mode = GPIO_MODE_DIGITAL
            enc.handle.config.mode = ENCODER_MODE_HALL
            enc.parent.save_config_and_reboot()
        else:
            time.sleep(1.0) # wait for PLLs to stabilize

        enc.handle.config.bandwidth = 100

        self.run_generic_encoder_test(enc.handle, true_cpr, true_rps)
        enc.handle.config.cpr = 8192



# This encoder emulation mimics the specification given in the following datasheets:
#
# With {mode} == ENCODER_MODE_SPI_ABS_CUI:
#   AMT23xx: https://www.cuidevices.com/product/resource/amt23.pdf
#
# With {mode} == ENCODER_MODE_SPI_ABS_AMS:
#   AS5047P: https://ams.com/documents/20143/36005/AS5047P_DS000324_2-00.pdf/a7d44138-51f1-2f6e-c8b6-2577b369ace8
#   AS5048A/AS5048B: https://ams.com/documents/20143/36005/AS5048_DS000298_4-00.pdf/910aef1f-6cd3-cbda-9d09-41f152104832
#   => Only the read command on address 0x3fff is currently implemented.

teensy_spi_encoder_emulation_code = """
#define ENCODER_MODE_SPI_ABS_CUI 0x100
#define ENCODER_MODE_SPI_ABS_AMS 0x101
#define ENCODER_MODE_SPI_ABS_AEAT 0x102

static float rps = 1.0f;
static uint32_t cpr = 16384;
static uint32_t us_per_revolution = (uint32_t)(1000000.0f / rps);
static uint16_t spi_txd = 0; // first output word: NOP
static uint32_t zerotime = 0;

void setup() {
  pinMode({ncs}, INPUT_PULLUP);
}

uint16_t get_pos_now() {
  uint32_t time = micros();
  return ((uint64_t)((time - zerotime) % us_per_revolution)) * cpr / us_per_revolution;
}


#if {mode} == ENCODER_MODE_SPI_ABS_AMS

uint8_t ams_parity(uint16_t v) {
  v ^= v >> 8;
  v ^= v >> 4;
  v ^= v >> 2;
  v ^= v >> 1;
  return v & 1;
}

uint16_t handle_command(uint16_t cmd) {
  const uint16_t ERROR_RESPONSE = 0xc000; // error flag and parity bit set
  
  if (ams_parity(cmd)) {
    return ERROR_RESPONSE;
  }

  if (!(cmd & 14)) { // write not supported
    return ERROR_RESPONSE;
  }

  uint16_t addr = cmd & 0x3fff;
  uint16_t data;

  switch (addr) {
    case 0x3fff: data = get_pos_now(); break;
    default: return ERROR_RESPONSE;
  }

  return data | (ams_parity(data) << 15);
}

#endif

#if {mode} == ENCODER_MODE_SPI_ABS_CUI

uint8_t cui_parity(uint16_t v) {
  v ^= v >> 8;
  v ^= v >> 4;
  v ^= v >> 2;
  return ~v & 3;
}

uint16_t handle_command(uint16_t cmd) {
  (void) cmd; // input not used on CUI

  // Test the cui_parity function itself with the example given in the datasheet
  if ((0x21AB | (cui_parity(0x21AB) << 14)) != 0x61AB) {
      return 0x0000;
  }

  uint16_t data = get_pos_now();
  return data | (cui_parity(data) << 14);
}

#endif


void loop() {
  while (digitalReadFast({reset})) {
    zerotime = micros();
  }

  if (!digitalReadFast({ncs})) {
    static uint16_t spi_rxd = 0;

    pinMode({miso}, OUTPUT);

    for (;;) {
      while (!digitalReadFast({sck}))
        if (digitalReadFast({ncs}))
          goto cs_deasserted;

      // Rising edge: Push output bit

      bool output_bit = spi_txd & 0x8000;
      digitalWriteFast({miso}, output_bit);
      spi_txd <<= 1;

      while (digitalReadFast({sck}))
        if (digitalReadFast({ncs}))
          goto cs_deasserted;

      // Falling edge: Sample input bit (only in AMS mode)

#if {mode} == ENCODER_MODE_SPI_ABS_AMS
      bool input_bit = digitalReadFast({mosi});
      spi_rxd <<= 1;
      if (input_bit) {
        spi_rxd |= 1;
      } else {
        spi_rxd &= ~1;
      }
#endif
    }

cs_deasserted:
    // chip deselected: Process command
    pinMode({miso}, INPUT);

    spi_txd = handle_command(spi_rxd);
  }
}
"""

class TestSpiEncoder(TestEncoderBase):
    def __init__(self, mode: int):
        self.mode = mode

    def get_test_cases(self, testrig: TestRig):
        for odrive in testrig.get_components(ODriveComponent):
            for encoder in odrive.encoders:
                odrive_ncs_gpio = odrive.gpio7 # this GPIO choice is completely arbitrary
                gpio_conns = [
                    testrig.get_connected_components(odrive.sck, TeensyGpio),
                    testrig.get_connected_components(odrive.miso, TeensyGpio),
                    testrig.get_connected_components(odrive.mosi, TeensyGpio),
                    testrig.get_connected_components(odrive_ncs_gpio, TeensyGpio),
                ]

                valid_combinations = []
                for combination in itertools.product(*gpio_conns):
                    if (len(set(c.parent for c in combination)) != 1):
                        continue
                    teensy = combination[0].parent
                    reset_pin_options = []
                    for gpio in teensy.gpios:
                        for local_gpio in testrig.get_connected_components(gpio, LinuxGpioComponent):
                            reset_pin_options.append((gpio, local_gpio))
                    valid_combinations.append((teensy, *combination, reset_pin_options))

                yield (encoder, 7, valid_combinations)


    def run_test(self, enc: ODriveEncoderComponent, odrive_ncs_gpio: int, teensy: TeensyComponent, teensy_gpio_sck: TeensyGpio, teensy_gpio_miso: TeensyGpio, teensy_gpio_mosi: TeensyGpio, teensy_gpio_ncs: TeensyGpio, teensy_gpio_reset: TeensyGpio, reset_gpio: LinuxGpioComponent, logger: Logger):
        true_cpr = 16384
        true_rps = 1.0
        
        reset_gpio.config(output=True) # hold encoder and disable its SPI
        reset_gpio.write(True)

        code = (teensy_spi_encoder_emulation_code
                .replace("{sck}", str(teensy_gpio_sck.num))
                .replace("{miso}", str(teensy_gpio_miso.num))
                .replace("{mosi}", str(teensy_gpio_mosi.num))
                .replace("{ncs}", str(teensy_gpio_ncs.num))
                .replace("{reset}", str(teensy_gpio_reset.num))
                .replace("{mode}", str(self.mode)))
        teensy.compile_and_program(code)

        logger.debug(f'Configuring absolute encoder in mode 0x{self.mode:x}...')
        enc.handle.config.mode = self.mode
        setattr(enc.parent.handle.config, 'gpio' + str(odrive_ncs_gpio) + '_mode', GPIO_MODE_ANALOG_IN)
        enc.handle.config.abs_spi_cs_gpio_pin = odrive_ncs_gpio
        enc.handle.config.cpr = true_cpr
        # Also put the other encoder into SPI mode to make it more interesting
        other_enc = enc.parent.encoders[1 - enc.num]
        other_enc.handle.config.mode = self.mode
        other_enc.handle.config.abs_spi_cs_gpio_pin = odrive_ncs_gpio
        other_enc.handle.config.cpr = true_cpr
        enc.parent.save_config_and_reboot()

        time.sleep(1.0)

        logger.debug('Testing absolute readings and SPI errors...')

        # Encoder is still disabled - expect recurring error
        enc.handle.error = 0
        time.sleep(0.002)
        # This fails from time to time because the pull-up on the ODrive only manages
        # to pull MISO to 1.8V, leaving it in the undefined range.
        test_assert_eq(enc.handle.error, ENCODER_ERROR_ABS_SPI_COM_FAIL)

        # Enable encoder and expect error to go away
        reset_gpio.write(False)
        release_time = time.monotonic()
        enc.handle.error = 0
        time.sleep(0.002)
        test_assert_eq(enc.handle.error, 0)

        # Check absolute position after 1.5s
        time.sleep(1.5)
        true_delta_t = time.monotonic() - release_time
        test_assert_eq(enc.handle.pos_abs, (true_delta_t * true_rps * true_cpr) % true_cpr, range = true_cpr*0.001)

        test_assert_eq(enc.handle.error, 0)
        reset_gpio.write(True)
        time.sleep(0.002)
        test_assert_eq(enc.handle.error, ENCODER_ERROR_ABS_SPI_COM_FAIL)
        reset_gpio.write(False)
        release_time = time.monotonic()
        enc.handle.error = 0
        time.sleep(0.002)
        test_assert_eq(enc.handle.error, 0)

        # Check absolute position after 1.5s
        time.sleep(1.5)
        true_delta_t = time.monotonic() - release_time
        test_assert_eq(enc.handle.pos_abs, (true_delta_t * true_rps * true_cpr) % true_cpr, range = true_cpr*0.001)

        self.run_generic_encoder_test(enc.handle, true_cpr, true_rps)
        enc.handle.config.cpr = 8192


teensy_uart_encoder_emulation_code = """
const float rps = 2.0;
const int update_rate = 4000;
const int baudrate = 921600;

float pos = 0.0f;

void setup() {
  Serial2.begin(baudrate);
}

void loop() {
  pos += rps / (float)update_rate;
  if (pos >= 1.0f)
    pos -= 1.0f;

  // Total length: 9 bytes
  //  => 781us @ 115200bps
  //  =>  98us @ 921600bps
  Serial2.print("{cmd} ");
  Serial2.print(pos, 4);
  Serial2.print("\\n");

  //delayMicroseconds(1000000.0f / (float)update_rate - 1000000.0f / (float)baudrate * 90.0f);
  delayMicroseconds(1000000.0f / (float)update_rate);
}
"""

class TestUartEncoder(TestEncoderBase):
    def get_test_cases(self, testrig: TestRig):
        for odrive in testrig.get_components(ODriveComponent):
            for encoder in odrive.encoders:
                # Find the Teensy that is connected to the encoder pins and the corresponding Teensy UART GPIOs

                gpio_conns = [
                    testrig.get_directly_connected_components(odrive.gpio1),
                    testrig.get_directly_connected_components(odrive.gpio2),
                ]

                valid_combinations = [
                    (combination[0].parent,)
                    for combination in itertools.product(*gpio_conns)
                    if ((len(set(c.parent for c in combination)) == 1) and isinstance(combination[0].parent, TeensyComponent)
                        and combination[0].num == 7 and combination[1].num == 8) # Must be RX2 and TX2
                ]

                yield (encoder, valid_combinations, 'a' if encoder.num == 0 else 'b')


    def run_test(self, enc: ODriveEncoderComponent, teensy: TeensyComponent, cmd: str, logger: Logger):
        code = teensy_uart_encoder_emulation_code.replace('{cmd}', cmd)
        teensy.compile_and_program(code)

        if enc.handle.config.mode != ENCODER_MODE_UART:
            enc.parent.disable_mappings()
            enc.parent.handle.config.gpio1_mode = GPIO_MODE_UART0
            enc.parent.handle.config.gpio2_mode = GPIO_MODE_UART0
            enc.parent.handle.config.uart0_baudrate = 921600
            enc.handle.config.mode = ENCODER_MODE_UART
            enc.parent.save_config_and_reboot()
        else:
            time.sleep(1.0) # wait for PLLs to stabilize

        enc.handle.config.bandwidth = 100

        true_rps = 1.9716 # in the Teensy code we have 2.0 but the timing is not 100% accurate
        self.run_generic_encoder_test(enc.handle, 6283, true_rps, 3.0)


if __name__ == '__main__':
    test_runner.run([
        TestIncrementalEncoder(),
        TestSinCosEncoder(),
        TestHallEffectEncoder(),
        TestSpiEncoder(ENCODER_MODE_SPI_ABS_AMS),
        TestSpiEncoder(ENCODER_MODE_SPI_ABS_CUI),
        TestUartEncoder(),
    ])
