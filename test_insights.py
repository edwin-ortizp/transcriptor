"""Live insight engine: local rules, call budget and AI fallback, with no network."""
import json
import tempfile
import unittest
from pathlib import Path

from insight_rules import InsightRules

import insights


def block(text, start=0.0, kind='text'):
    return {'type':kind, 'text':text, 'start':start, 'time':'00:00:%02d' % int(start), 'source':'Otros'}


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value


class Engine:
    def __init__(self, reply=None, error=None, **config):
        self.events, self.calls, self.clock = [], [], Clock()
        def complete(system, text, max_tokens):
            self.calls.append(text)
            if error: raise error
            return json.dumps(reply or {'show':False}), {'prompt_tokens':10, 'completion_tokens':5}
        self.engine = insights.InsightEngine(config, self.events.append, complete, self.clock, spawn=lambda fn: fn())

    def shown(self):
        return [e['insight'] for e in self.events if e['type'] == 'insight']


class DetectorTest(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(insights.detect('Entonces decidimos ir con la opción B')[0], 'decision')
        self.assertEqual(insights.detect('Yo me encargo de enviarlo para el viernes')[0], 'commitment')
        self.assertEqual(insights.detect('El problema es que estamos bloqueados')[0], 'risk')
        self.assertEqual(insights.detect('Tengo otra duda')[0], 'question')
        self.assertIsNone(insights.detect('¿Listo?'))  # bare question marks never count
        self.assertIsNone(insights.detect('Buenos días a todos'))

    def test_numbers_add_score(self):
        self.assertGreater(insights.detect('Decidimos invertir 20 millones')[1], insights.detect('Decidimos invertir')[1])

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            insights.validate_config({'mode':'siempre'})
        with self.assertRaises(ValueError):
            insights.validate_config({'max_calls':-1})


class LocalModeTest(unittest.TestCase):
    def test_local_shows_without_calls_and_deduplicates(self):
        t = Engine(mode='local')
        t.engine.feed(block('Decidimos lanzar el lunes'))
        t.engine.feed(block('Decidimos lanzar el lunes.', 5))
        t.engine.feed(block('Hola a todos', 6))
        self.assertEqual(len(t.shown()), 1)
        self.assertEqual(t.shown()[0]['source'], 'local')
        self.assertEqual(t.calls, [])

    def test_off_ignores_everything(self):
        t = Engine(mode='off')
        t.engine.feed(block('Decidimos lanzar el lunes'))
        self.assertEqual(t.events, [])


class AiModeTest(unittest.TestCase):
    def test_threshold_cooldown_and_cap(self):
        t = Engine(reply={'show':True, 'type':'decision', 'text':'Se lanza el lunes', 'ref':'00:00:01'},
                   mode='ai', threshold=4, cooldown_seconds=90, max_calls=2)
        t.engine.feed(block('¿Lo vemos?', 0))
        self.assertEqual(t.calls, [])  # below threshold
        t.engine.feed(block('Decidimos lanzar el lunes, es urgente', 1))
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.shown()[0]['source'], 'ai')
        self.assertEqual(t.shown()[0]['start'], 1)
        t.engine.feed(block('Acordamos el presupuesto, es urgente', 10))
        self.assertEqual(len(t.calls), 1)  # cooling down
        t.clock.value = 100
        t.engine.feed(block('Queda aprobado el plan, hay riesgo', 110))
        self.assertEqual(len(t.calls), 2)
        t.clock.value = 300
        t.engine.feed(block('Decidimos contratar, es crítico', 300))
        self.assertEqual(len(t.calls), 2)  # cap reached
        self.assertEqual(t.engine.state()['usage']['prompt_tokens'], 20)

    def test_show_false_displays_nothing(self):
        t = Engine(mode='ai', threshold=1)
        t.engine.feed(block('Decidimos lanzar el lunes'))
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.shown(), [])

    def test_ai_error_degrades_to_local(self):
        t = Engine(error=RuntimeError('caído'), mode='ai', threshold=1)
        t.engine.feed(block('Decidimos lanzar el lunes'))
        self.assertTrue(t.engine.state()['degraded'])
        self.assertEqual(t.shown()[0]['source'], 'local')
        t.engine.feed(block('Me comprometo a enviarlo mañana', 5))
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(len(t.shown()), 2)

    def test_prompt_lists_previous_insights(self):
        t = Engine(reply={'show':True, 'type':'risk', 'text':'Proveedor retrasado', 'ref':None},
                   mode='ai', threshold=1, cooldown_seconds=10)
        t.engine.feed(block('El proveedor está retrasado'))
        t.clock.value = 20
        t.engine.feed(block('Decidimos cambiar de proveedor', 20))
        self.assertIn('Proveedor retrasado', t.calls[1])
        self.assertEqual(len(t.shown()), 1)  # duplicate reply is not shown twice

    def test_now_skips_threshold(self):
        t = Engine(mode='ai', threshold=50)
        t.engine.feed(block('¿Quién lo revisa?'))
        self.assertTrue(t.engine.now())
        self.assertEqual(len(t.calls), 1)

    def test_pin_and_dismiss(self):
        t = Engine(mode='local')
        t.engine.feed(block('Decidimos lanzar el lunes'))
        item = t.shown()[0]
        self.assertTrue(t.engine.update(item['id'], pinned=True)['pinned'])
        self.assertTrue(t.engine.update(item['id'], dismissed=True)['dismissed'])
        with self.assertRaises(KeyError):
            t.engine.update('nope', pinned=True)


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user = Path(self.tmp.name) / 'user.json'
        self.rules = InsightRules(insights.BASE_RULES, self.user)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accents_and_case_do_not_matter(self):
        self.assertEqual(self.rules.detect('Es CRÍTICO terminar')['type'], 'risk')
        self.assertEqual(self.rules.detect('Lo hacemos para el miércoles')['type'], 'commitment')

    def test_exclusions_beat_rules(self):
        self.assertIsNone(self.rules.detect('No hay problema con eso'))
        self.assertIsNotNone(self.rules.detect('Hay un problema con eso'))
        self.rules.add_exclusion('problema de sonido')
        self.assertIsNone(self.rules.detect('Perdón, un problema de sonido'))
        self.rules.remove_exclusion('no hay problema')
        self.assertIsNotNone(self.rules.detect('No hay problema con eso'))

    def test_user_rules_add_disable_and_delete(self):
        self.assertIsNone(self.rules.detect('Lo cerramos así'))
        rule = self.rules.add_rule('decision', 'Lo cerramos así', 3)
        self.assertEqual(self.rules.detect('Bueno, lo cerramos asi')['rule_ids'], [rule['id']])
        with self.assertRaises(ValueError):
            self.rules.add_rule('decision', 'lo cerramos ASÍ')
        self.rules.update_rule(rule['id'], enabled=False)
        self.assertIsNone(self.rules.detect('Lo cerramos así'))
        self.rules.delete_rule(rule['id'])
        self.assertFalse(any(r['id'] == rule['id'] for r in self.rules.listing()['rules']))

    def test_whole_words_only(self):
        self.rules.add_rule('risk', 'caida')
        self.assertIsNone(self.rules.detect('Fue una caída-libre'.replace('caída-', 'caídas')))

    def test_base_rules_are_disabled_not_deleted(self):
        self.rules.delete_rule('base-risk-1')  # "problema"
        self.assertIsNone(self.rules.detect('Hay un problema'))
        self.assertTrue(any(r['id'] == 'base-risk-1' for r in self.rules.listing()['rules']))
        self.rules.update_rule('base-risk-1', enabled=True)
        self.assertIsNotNone(self.rules.detect('Hay un problema'))

    def test_zero_weight_mutes_rule(self):
        self.rules.update_rule('base-question-signo', weight=0)
        self.assertIsNone(self.rules.detect('¿Seguimos?'))

    def test_flagging_noisy_rules(self):
        for _ in range(5): self.rules.record(['base-risk-1'], 'hits')
        for _ in range(4): self.rules.record(['base-risk-1'], 'dismissed')
        row = next(r for r in self.rules.listing()['rules'] if r['id'] == 'base-risk-1')
        self.assertTrue(row['flagged'])

    def test_user_changes_survive_reload(self):
        self.rules.add_rule('commitment', 'yo lo hago')
        fresh = InsightRules(insights.BASE_RULES, self.user)
        self.assertEqual(fresh.detect('Yo lo hago')['type'], 'commitment')

    def test_read_only_without_user_file(self):
        rules = InsightRules(insights.BASE_RULES)
        rules.record(['base-risk-1'], 'hits')
        self.assertIsNone(rules.listing()['user_path'])

    def test_replay_counts_saved_sessions(self):
        result = self.rules.replay([{'session_id':'a', 'history':[block('Decidimos lanzar'), block('Hola')]}])
        self.assertEqual(result['total'], 1)


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rules = InsightRules(insights.BASE_RULES, Path(self.tmp.name) / 'user.json')

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, **kw):
        events = []
        reply = kw.pop('reply', {'show':False})
        engine = insights.InsightEngine({'mode':'local', **kw}, events.append,
                                        lambda *a: (json.dumps(reply), {}), spawn=lambda fn: fn(), rules=self.rules)
        return engine, events

    def stats(self, rule_id):
        return next(r for r in self.rules.listing()['rules'] if r['id'] == rule_id)['stats']

    def test_hits_pins_and_false_positives_are_counted(self):
        engine, events = self.engine()
        engine.feed(block('Hay un bloqueo'))
        item = events[-1]['insight']
        self.assertEqual(item['rule_ids'], ['base-risk-4'])
        self.assertEqual(item['phrases'], ['bloqueo'])
        engine.update(item['id'], pinned=True)
        engine.false_positive(item['id'])
        engine.false_positive(item['id'])  # counted once
        self.assertEqual(self.stats('base-risk-4'), {'hits':1, 'pinned':1, 'dismissed':1})
        self.assertTrue(engine.public()[0]['dismissed'])

    def test_ai_finding_missed_locally_becomes_suggestion(self):
        engine, _ = self.engine(mode='ai', threshold=1,
                                reply={'show':True, 'type':'commitment', 'text':'Ana envía el contrato', 'ref':'00:00:02'})
        engine.feed(block('Ana manda el contrato', 2))
        engine.feed(block('Tengo otra duda del presupuesto', 3))
        suggestions = self.rules.listing()['suggestions']
        self.assertEqual([s['text'] for s in suggestions], ['Ana manda el contrato'])

    def test_rule_changes_apply_to_running_engine(self):
        engine, events = self.engine()
        self.rules.add_exclusion('el problema es el wifi')
        engine.feed(block('El problema es el wifi'))
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
