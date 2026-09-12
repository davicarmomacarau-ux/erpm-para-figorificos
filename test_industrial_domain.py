import unittest
from datetime import datetime, timedelta

from app import (
    ScaleParser,
    ScaleReading,
    CoolingBreakService,
    CoolingBreakInput,
    CoolingBreakOutput,
    CoolingBreakException,
    CoolingBreakToleranceAlert,
)


class IndustrialDomainTests(unittest.TestCase):
    def test_scale_parser_parses_stable_scale_line(self):
        line = "@ESTAVEL=VERDADEIRO;BRUTO=120.500;TARA=2.000;LIQUIDO=118.500;UNIDADE=KG"
        reading = ScaleParser.parse(line)

        self.assertIsInstance(reading, ScaleReading)
        self.assertTrue(reading.estavel)
        self.assertEqual(reading.pesoBruto, 120.5)
        self.assertEqual(reading.tara, 2.0)
        self.assertEqual(reading.pesoLiquido, 118.5)
        self.assertEqual(reading.unidade, 'KG')

    def test_cooling_break_calculation_and_alert(self):
        entrada = datetime(2026, 9, 1, 8, 0, 0)
        saida = entrada + timedelta(hours=30)

        dto = CoolingBreakInput(
            carcaca_id=1,
            peso_quente=100.0,
            peso_frio=98.5,
            entrada=entrada,
            saida=saida,
        )

        output = CoolingBreakService.calculate(dto)

        self.assertIsInstance(output, CoolingBreakOutput)
        self.assertAlmostEqual(output.quebra_absoluta_kg, 1.5)
        self.assertAlmostEqual(output.percentual_quebra, 1.5)
        self.assertEqual(output.tempo_resfriamento_horas, 30.0)
        self.assertIsNone(output.alerta_sif)

    def test_cooling_break_invalid_cooling_time_raises_exception(self):
        entrada = datetime(2026, 9, 1, 8, 0, 0)
        saida = entrada + timedelta(hours=23)

        dto = CoolingBreakInput(
            carcaca_id=1,
            peso_quente=100.0,
            peso_frio=98.5,
            entrada=entrada,
            saida=saida,
        )

        with self.assertRaises(CoolingBreakException):
            CoolingBreakService.calculate(dto)


if __name__ == '__main__':
    unittest.main()
