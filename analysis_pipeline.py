"""Budgeted, resumable analysis stages. No provider calls or retries live here."""
import hashlib
import json
import math


VERSION = 2


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


class BudgetError(ValueError):
    pass


class Budget:
    def __init__(self, config):
        self.config = config
        encoding = config.get('tokenizer_encoding', 'conservative')
        self.encoding = None
        if encoding != 'conservative':
            try:
                import tiktoken
                self.encoding = tiktoken.get_encoding(encoding)
            except Exception:
                raise BudgetError('No se pudo cargar el contador seleccionado. Instala tiktoken y prepara su caché, o selecciona la estimación UTF-8.') from None
        self.label = encoding if self.encoding else 'Estimación conservadora UTF-8'

    def tokens(self, text):
        # Encoding is explicit: never infer compatibility from a model name.
        return len(self.encoding.encode(text, disallowed_special=())) if self.encoding else len(text.encode('utf-8'))

    def input_tokens(self, system, text):
        # Protocol allowance; providers may use different message wrappers.
        return 64 + self.tokens(system) + self.tokens(text)

    def fits(self, system, text, output):
        total = self.input_tokens(system, text) + output
        return (total <= math.floor(self.config['context_window_tokens'] * .85)
                and (not self.config.get('tokens_per_minute') or total <= self.config['tokens_per_minute']))

    def per_token(self):
        """Measured units per real output token for a model that honours max_tokens."""
        return 1.0 if self.encoding else 4.0

    def words(self, measured):
        """Approximate Spanish words that fit in a measured size."""
        return max(20, int(measured * .75 if self.encoding else measured / 6))

    def output(self, stage):
        key = {'extract':'partial_output_tokens', 'consolidate':'consolidation_output_tokens',
               'compact':'consolidation_output_tokens', 'final':'final_output_tokens'}[stage]
        return min(self.config[key], self.config['max_output_tokens'])


def instructions(system, sections, stage):
    base = system + '\nEstructura requerida: ' + sections
    if stage == 'extract':
        return base + ('\nExtrae notas compactas de esta sección, no un informe completo. Prioriza hechos, '
                       'decisiones, compromisos, dudas y conceptos relevantes. Conserva referencias temporales '
                       'y diferencias; omite introducciones y secciones vacías. Ajusta el detalle al límite de salida '
                       'y termina la respuesta completa dentro de él.')
    if stage == 'final':
        return base + ('\nEscribe una síntesis global compacta de las notas. El detalle se conserva por secciones '
                       'y no debes repetirlo íntegro. Conserva referencias y diferencias. No añadas hechos nuevos. '
                       'Termina la respuesta completa dentro del límite de salida.')
    return base + ('\nConsolida estas notas en notas más breves, sin introducciones ni secciones vacías. '
                   'Conserva decisiones, compromisos, incertidumbres y referencias. No añadas hechos nuevos. '
                   'El material original y el detalle seguirán disponibles. Termina dentro del límite de salida.')


def source_sections(data, budget, system):
    """Pack complete source units and markers, splitting oversized units without dropping text."""
    context = json.dumps({'contexto':data.get('context', '')}, ensure_ascii=False)
    prefix = 'Ayudas del usuario (no son testimonio):\n' + context + '\n\nMaterial:\n'
    output = budget.output('extract')
    if not budget.fits(system, prefix + '[00:00:00] Texto mínimo.', output):
        raise BudgetError('El contexto, las instrucciones y la salida reservada no caben. Reduce el contexto o la salida, o aumenta la ventana configurada.')
    units = []
    for index, item in enumerate(data.get('history', [])):
        if item.get('type') == 'text' and item.get('text'):
            units.append({'start':float(item.get('start', 0)), 'end':float(item.get('end', item.get('start', 0))),
                          'ref':f"[{item['time']}] [{item.get('source','Otros')}] ",
                          'text':item['text'], 'id':item.get('id', f'block-{index}')})
    if not units:
        raise BudgetError('La sesión no tiene texto para analizar')
    for index, marker in enumerate(data.get('markers', [])):
        start = float(marker.get('start', marker.get('time_seconds', 0)))
        # Current manifests use numeric "time" for markers.
        if isinstance(marker.get('time'), (int, float)):
            start = float(marker['time'])
        seconds = int(start)
        stamp = f'{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}'
        units.append({'start':start, 'end':start, 'ref':f'[{stamp}] [Marcador; ayuda del usuario] ',
                      'text':marker.get('label', marker.get('text', 'Momento')), 'id':marker.get('id', f'marker-{index}')})
    units.sort(key=lambda item:item['start'])
    sections, current, ids, start, end = [], '', [], 0, 0

    def flush():
        nonlocal current, ids
        if current:
            sections.append({'text':prefix + current, 'start':start, 'end':end, 'source_ids':ids})
            current, ids = '', []

    for item in units:
        remaining = item['text']
        while remaining:
            separator = '\n' if current else ''
            def fits(n):
                candidate = current + separator + item['ref'] + remaining[:n]
                return len(candidate) <= budget.config['chunk_chars'] and budget.fits(system, prefix + candidate, output)
            if not fits(1):
                if current:
                    flush()
                    continue
                raise BudgetError('Una referencia y el contexto no caben en la entrada configurada; aumenta el presupuesto de entrada.')
            low, high = 1, len(remaining)
            while low < high:
                mid = (low + high + 1) // 2
                if fits(mid): low = mid
                else: high = mid - 1
            if not current:
                start = item['start']
            current += separator + item['ref'] + remaining[:low]
            end = max(start, end if ids else start, item['end'])
            if item['id'] not in ids:
                ids.append(item['id'])
            remaining = remaining[low:]
            if remaining:
                flush()
    flush()
    return sections


def fingerprint(data, config, prompt_version, system, sections):
    excluded = {'automatic','requests_per_minute','tokens_per_minute','max_calls',
                'timeout_seconds','input_price','output_price','max_output_tokens',
                'partial_output_tokens','consolidation_output_tokens','final_output_tokens'}
    # Quotas affect packing, so the material plan is frozen separately at first run.
    return digest({'text':data.get('history'), 'context':data.get('context'), 'markers':data.get('markers'),
                   'config':{k:v for k,v in config.items() if k not in excluded},
                   'prompt_version':prompt_version, 'instructions':system, 'sections':sections})


def notes_payload(nodes):
    return '\n\n'.join(f"Sección {index+1}:\n{node['text']}" for index,node in enumerate(nodes))


class Pipeline:
    def __init__(self, data, config, checkpoint, system, sections, execute):
        self.data, self.config, self.checkpoint = data, config, checkpoint
        self.budget = Budget(config)
        self.systems = {stage:instructions(system, sections, stage) for stage in ('extract','consolidate','compact','final')}
        self.execute = execute

    def prepare(self):
        if 'source_sections' not in self.checkpoint:
            self.checkpoint['source_sections'] = source_sections(self.data, self.budget, self.systems['extract'])
        return self.checkpoint['source_sections']

    def node(self, node_id, stage, text, deps, start=0, end=0, output=None, words=None):
        output = output or self.budget.output(stage)
        system = self.systems[stage]
        if stage != 'extract':
            # Some providers ignore max_tokens; a length instruction in the prompt is what they follow.
            words = words or self.budget.words(self.pair_room() if stage == 'consolidate'
                                               else output * self.budget.per_token())
            system += f'\nExtensión máxima: unas {words} palabras. Es un límite estricto.'
        spec = {'id':node_id, 'stage':stage, 'dependencies':deps, 'start':start, 'end':end,
                'input_hash':digest([system, text, output]), 'output_budget':output,
                'estimated_input_tokens':self.budget.input_tokens(system, text)}
        return self.execute(spec, system, text)

    def reserved(self, stage):
        # Room for the length instruction that node() appends to non-extract prompts.
        return self.systems[stage] + ' ' * 80

    def pair_room(self):
        """Measured size per note so that two notes fit together in one consolidation."""
        limit = math.floor(self.config['context_window_tokens'] * .85)
        free = limit - self.budget.input_tokens(self.reserved('consolidate'), '') - self.budget.output('consolidate')
        return max(64, free // 2 - 32)

    def overshoot(self):
        """Median of produced size versus requested output, relative to a compliant model (1.0)."""
        ratios = sorted(self.budget.tokens(n['text']) / (n['output_budget'] * self.budget.per_token())
                        for n in self.checkpoint.get('nodes', {}).values()
                        if n.get('state') == 'completed' and n.get('output_budget') and n.get('text'))
        value = max(1.0, ratios[len(ratios) // 2]) if ratios else 1.0
        self.checkpoint['overshoot'] = round(value, 2)
        return value

    def stuck(self, base):
        factor = self.overshoot()
        if factor > 1.5:
            return (f'{base} El modelo devuelve unas {factor:.1f} veces más texto del solicitado e ignora el límite de salida. '
                    'Si su ventana real es mayor, aumenta la ventana de contexto del perfil; si no, usa otro modelo.')
        return base

    def compact(self, nodes, level, round_=0):
        # Each round halves the goal; the goal lets two compacted notes share one consolidation.
        room = self.pair_room() >> round_
        ids = [f'compact:{level}:{round_}:{index}' for index in range(len(nodes))]
        pending = sum(self.checkpoint.get('nodes', {}).get(i, {}).get('state') != 'completed' for i in ids)
        if self.config.get('max_calls') and len(self.checkpoint.get('requests', [])) + pending + 1 > self.config['max_calls']:
            raise BudgetError(f'Compactar las notas requiere al menos {pending + 1} llamadas más y se superaría el máximo '
                              f'de {self.config["max_calls"]}. Aumenta el máximo de llamadas y continúa: las etapas guardadas se reutilizan.')
        ratio = self.budget.per_token() * self.overshoot()
        compacted = []
        for node_id, node in zip(ids, nodes):
            goal = max(48, min(room, self.budget.tokens(node['text']) // 2))
            output = max(32, min(self.budget.output('compact'), int(goal / ratio)))
            compacted.append(self.node(node_id, 'compact', notes_payload([node]), [node['id']],
                                       node['start'], node['end'], output, self.budget.words(goal)))
        if sum(self.budget.tokens(n['text']) for n in compacted) >= sum(self.budget.tokens(n['text']) for n in nodes):
            raise BudgetError(self.stuck('El modelo no redujo las notas tras la compactación adicional. Se conserva el detalle.'))
        return compacted

    def run(self):
        material = self.prepare()
        nodes = [self.node(f'extract:{index}', 'extract', item['text'], item['source_ids'], item['start'], item['end'])
                 for index,item in enumerate(material)]
        details = nodes[:]
        level, rounds = 0, 0
        while True:
            if self.budget.fits(self.reserved('final'), notes_payload(nodes), self.budget.output('final')):
                final = self.node('final', 'final', notes_payload(nodes), [n['id'] for n in nodes], nodes[0]['start'], max(n['end'] for n in nodes))
                return final['text'], details
            groups, group, oversized = [], [], False
            for node in nodes:
                if not self.budget.fits(self.reserved('consolidate'), notes_payload([node]), self.budget.output('consolidate')):
                    oversized = True
                    break
                if group and not self.budget.fits(self.reserved('consolidate'), notes_payload(group + [node]), self.budget.output('consolidate')):
                    groups.append(group)
                    group = []
                group.append(node)
            if group:
                groups.append(group)
            if oversized or len(groups) >= len(nodes):
                if rounds >= 3:
                    raise BudgetError(self.stuck('Las notas siguen sin caber después de compactarlas. El detalle está guardado; revisa los presupuestos del perfil.'))
                nodes = self.compact(nodes, level, rounds)
                rounds += 1
                continue
            previous_size = sum(self.budget.tokens(n['text']) for n in nodes)
            merged = []
            for index, group in enumerate(groups):
                merged.append(group[0] if len(group) == 1 else self.node(f'consolidate:{level}:{index}', 'consolidate',
                              notes_payload(group), [n['id'] for n in group], group[0]['start'], max(n['end'] for n in group)))
            if sum(self.budget.tokens(n['text']) for n in merged) >= previous_size:
                merged = self.compact(merged, level + 1)
                if sum(self.budget.tokens(n['text']) for n in merged) >= previous_size:
                    raise BudgetError(self.stuck('El nivel no redujo el volumen original después de compactar. Se conserva el detalle; revisa el perfil o el modelo.'))
            nodes = merged
            level += 1
            rounds = 0
            if level > 32:
                raise BudgetError('Se alcanzó el límite de niveles de consolidación; los avances están guardados.')
