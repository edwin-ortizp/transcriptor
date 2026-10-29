"""Optional Windows UI validation using Playwright and synthetic devices only.

Run: python tools/validate_session_ui.py
Screenshots are written to a temporary folder, never to personal sessions.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import uvicorn
from playwright.sync_api import sync_playwright, expect

import app
import corrections


class Recorder:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def record(self, numframes):
        time.sleep(numframes / app.audio.CAPTURE_RATE)
        t = np.arange(numframes) / app.audio.CAPTURE_RATE
        return (.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32).reshape(-1, 1)


class Device:
    name = 'Audio sintético'
    def recorder(self, **_): return Recorder()


def validate_native(url, evidence):
    import webview
    from PIL import ImageGrab
    from desktop import Desktop
    failures = []
    def drain():
        if app.session.worker: app.session.worker.join(5)
    shell = Desktop(url, lambda:app.session.stop(), drain, app.analysis_queue.stop, app.probe.stop)
    app.desktop, app.window = shell, shell.widget
    shell.widget.on_top = True
    shell.start_tray()
    def check():
        def js(window, code): return window.evaluate_js(code)
        def wait_for(window, code):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if js(window,code): return
                except Exception: pass
                time.sleep(.1)
            raise AssertionError('Native condition failed: ' + code)
        def screenshot(window,name):
            ImageGrab.grab(bbox=(window.x,window.y,window.x+window.width,window.y+window.height)).save(evidence/name)
        try:
            widget,manager = shell.widget,shell.manager
            wait_for(widget,"typeof isWindow !== 'undefined' && isWindow && document.getElementById('model').value === 'fake'")
            js(widget,"document.getElementById('sessions').click()")
            wait_for(manager,"typeof native !== 'undefined' && native && document.querySelector('.session-row') !== null")
            shell.show_manager(); assert len(webview.windows) == 2
            js(manager,"document.getElementById('showDictionary').click()")
            wait_for(manager,"document.getElementById('dictionaryDialog').open && document.querySelector('#termRows tr') !== null")
            js(manager,"document.getElementById('termSearch').value='Colombia'; document.getElementById('termSearch').dispatchEvent(new Event('input'))")
            wait_for(manager,"document.getElementById('termRows').textContent.includes('Colombia')")
            js(manager,"document.querySelector('[data-dictionary-view=mappings]').click()")
            wait_for(manager,"document.getElementById('mappingRows').textContent.includes('si go')")
            js(manager,"document.getElementById('addMapping').click(); document.getElementById('mappingSource').value='se ge si'; document.getElementById('mappingTarget').value='SGSI'; document.getElementById('mappingForm').requestSubmit()")
            wait_for(manager,"document.getElementById('mappingRows').textContent.includes('se ge si') && document.getElementById('mappingForm').hidden")
            screenshot(manager,'native-dictionary.png')
            js(manager,"document.querySelector('[aria-label=\"Cerrar diccionario\"]').click()")
            js(manager,"loadDetail('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',true)")
            wait_for(manager,"detail && detail.session_id === 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'")
            js(manager,"document.querySelector('[data-tab=suspects]').click()")
            wait_for(manager,"document.getElementById('reviewState').textContent.includes('Pulsa Buscar')")
            js(manager,"document.getElementById('scanWords').click()")
            wait_for(manager,"document.getElementById('reviewState').textContent.includes('3 pendientes')")
            js(manager,"[...document.querySelectorAll('.review-card')].find(c=>c.querySelector('strong').textContent==='zqword').querySelector('.button-row button').click()")
            wait_for(manager,"document.getElementById('reviewState').textContent.includes('2 pendientes')")
            screenshot(manager,'native-review-words.png')
            js(manager,"document.querySelector('[data-tab=transcript]').click()")
            # Exercise finished-session mutations before preparing a new recording.
            path=app.current_corrections().paths[0]
            with path.open('a',encoding='utf-8') as file:file.write('qzthird\tCorrecto\tword\tnocase\n')
            wait_for(manager,"!document.getElementById('renameSession').disabled")
            js(manager,"document.getElementById('renameSession').click()")
            wait_for(manager,"document.getElementById('renameDialog').open")
            js(manager,"document.getElementById('savedTitle').value='Reunión nativa renombrada'; document.getElementById('savedTitle').dispatchEvent(new Event('input')); document.getElementById('renameForm').requestSubmit()")
            wait_for(manager,"document.getElementById('sessionTitle').textContent==='Reunión nativa renombrada' && !document.getElementById('renameDialog').open")
            js(manager,"document.getElementById('reapplyMappings').click()")
            wait_for(manager,"document.getElementById('textEditChanges').textContent.includes('Correcto') && !document.getElementById('confirmTextEdit').disabled")
            screenshot(manager,'native-text-preview.png')
            js(manager,"document.getElementById('confirmTextEdit').click()")
            wait_for(manager,"!document.getElementById('textEditDialog').open && document.getElementById('transcript').textContent.includes('Correcto')")
            js(manager,"document.getElementById('restoreTranscript').click()")
            wait_for(manager,"!document.getElementById('confirmTextEdit').disabled && document.getElementById('textEditHeading').textContent.includes('Restaurar')")
            js(manager,"document.getElementById('confirmTextEdit').click()")
            wait_for(manager,"!document.getElementById('textEditDialog').open && document.getElementById('transcript').textContent.includes('qzthird')")
            js(manager,"document.getElementById('prepare').click()")
            wait_for(manager,"document.getElementById('preparationDialog').open")
            js(manager,"document.getElementById('draftTitle').value = 'Sesión nativa'; document.getElementById('draftContext').value = 'Contexto sintético'; document.getElementById('draftBackup').checked = true; document.getElementById('draftTemplate').value = 'interview'; document.getElementById('probeStart').click()")
            wait_for(manager,"document.getElementById('probeResults').textContent.includes('Señal detectada')")
            js(manager,"document.getElementById('preparationForm').requestSubmit()")
            wait_for(manager,"!document.getElementById('preparationDialog').open")
            js(widget,"document.getElementById('toggle').click()")
            wait_for(widget,"sessionState === 'recording' && document.querySelector('#log .line') !== null")
            sid = app.session.session_id
            js(widget,"document.getElementById('mark').click()")
            shell.widget.resize(440,620)
            wait_for(widget,"Math.abs(innerWidth-440)<4")
            expanded=(shell.widget.width,shell.widget.height)
            js(manager,"document.getElementById('privacy').click()")
            wait_for(widget,"document.body.classList.contains('discreet')")
            wait_for(widget,"Math.abs(innerHeight-64)<4 && Math.abs(innerWidth-320)<4")
            assert abs(shell.widget.width-320)<4 and abs(shell.widget.height-64)<4
            js(widget,"setPrivate(true);setPrivate(true)")
            shell.widget.move(250,120)
            screenshot(widget,'native-widget-private.png')
            js(manager,"document.getElementById('privacy').click()")
            wait_for(widget,"!document.body.classList.contains('discreet') && Math.abs(innerWidth-440)<4")
            assert abs(shell.widget.width-expanded[0])<4 and abs(shell.widget.height-expanded[1])<4
            assert abs(shell.widget.x-250)<4 and abs(shell.widget.y-120)<4
            # Closing the recording widget must finalize capture while the tray survives.
            assert shell.widget.events.closing.set() is True
            wait_for(manager,"document.getElementById('captureState').textContent === 'Finalizada'")
            deadline = time.monotonic() + 15
            while app.repository.analysis(sid).get('state') != 'completed' and time.monotonic() < deadline: time.sleep(.1)
            saved_analysis = app.repository.analysis(sid)
            assert saved_analysis.get('state') == 'completed', {'analysis':saved_analysis,
                'automatic':app.analysis_config()['automatic'], 'capture_failure':app.session.failure_text,
                'archive_error':app.session.archive_error, 'jobs':len(app.analysis_queue.jobs)}
            js(manager,f"loadDetail('{sid}',true)")
            wait_for(manager,"!document.getElementById('playback').hidden")
            js(manager,"document.querySelector('[data-tab=analysis]').click(); document.getElementById('showAnalysisDetails').click()")
            wait_for(manager,"document.querySelector('#analysisDetails .analysis-section') !== null")
            js(manager,"document.querySelector('#analysisDetails summary').click()")
            screenshot(manager,'native-analysis-sections.png')
            from test_analysis import Provider
            Provider.status = 429
            js(manager,"document.getElementById('analyze').click()")
            wait_for(manager,"document.getElementById('analysisState').textContent.includes('HTTP 429') && !document.getElementById('continueAnalysis').hidden")
            Provider.status = 200
            js(manager,"document.getElementById('continueAnalysis').click()")
            wait_for(manager,"document.getElementById('analysisState').textContent.startsWith('Completado')")
            js(manager,"document.querySelector('[data-tab=transcript]').click()")
            screenshot(manager,'native-manager.png')
            assert shell.manager.events.closing.set() is True
            shell.show_manager(); assert len(webview.windows) == 2
            shell.show_widget()
            screenshot(widget,'native-widget.png')
            assert shell.tray is not None
        except Exception as exc:
            try:screenshot(shell.manager,'native-failure.png')
            except Exception:pass
            failures.append(str(exc))
        finally:
            shell.exit()
    # shown is emitted again when restored; protect the test from running twice.
    original_check = check
    def once():
        if getattr(once,'started',False): return
        once.started = True
        original_check()
    shell.widget.events.shown += lambda: threading.Thread(target=once,daemon=True).start()
    webview.start()
    if failures: raise AssertionError(failures)
    print(json.dumps({'result':'OK','surface':'Native Windows WebView2 + tray','screenshots':str(evidence)},ensure_ascii=False))


def validate_dictionary_browser(manager,widget,evidence):
    manager.locator('#showDictionary').click()
    manager.locator('#termSearch').fill('Colombia')
    expect(manager.locator('#termRows')).to_contain_text('Colombia')
    assert manager.locator('#dictionarySystem button').count()==2  # pagination only
    manager.locator('#termSource').select_option('en');manager.locator('#termSearch').fill('computer')
    expect(manager.locator('#termRows')).to_contain_text('computer')
    manager.locator('#termSource').select_option('own')
    expect(manager.locator('#termRows')).to_contain_text('No hay términos')
    manager.locator('[data-dictionary-view=mappings]').click()
    expect(manager.locator('#mappingRows')).to_contain_text('si go')
    manager.locator('#addMapping').click()
    manager.locator('#mappingSource').fill('se ge si')
    manager.locator('#mappingTarget').fill('SGSI')
    manager.locator('#mappingTarget').press('Enter')
    expect(manager.locator('#mappingForm')).not_to_be_visible()
    row=manager.locator('#mappingRows tr').filter(has=manager.get_by_text('se ge si',exact=True))
    expect(row).to_contain_text('SGSI')
    row.get_by_role('button',name='Editar',exact=True).click()
    manager.locator('#mappingTarget').fill('SGSI nuevo')
    path=app.current_corrections().paths[0]
    with path.open('a',encoding='utf-8') as file:file.write('# Cambio externo sintético\n')
    manager.locator('#saveMapping').click()
    expect(manager.locator('#mappingError')).to_contain_text('cambió')
    expect(manager.locator('#mappingTarget')).to_have_value('SGSI nuevo')
    manager.locator('#reloadMappings').click()
    expect(manager.locator('#mappingError')).to_contain_text('se conserva tu borrador')
    expect(manager.locator('#mappingTarget')).to_have_value('SGSI nuevo')
    manager.locator('#saveMapping').click()
    expect(manager.locator('#mappingForm')).not_to_be_visible()
    expect(row).to_contain_text('SGSI nuevo')
    manager.locator('.mapping-preview summary').click()
    manager.locator('#mappingTestText').fill('SI GO y se ge si')
    manager.locator('#testMappings').click()
    expect(manager.locator('#mappingTestResult')).to_have_text('Siigo y SGSI nuevo')
    manager.screenshot(path=str(evidence/'dictionary-mappings.png'))
    manager.set_viewport_size({'width':760,'height':500})
    manager.screenshot(path=str(evidence/'dictionary-760.png'))
    box=manager.locator('#dictionaryDialog').bounding_box();assert box['y']+box['height']<=500
    assert manager.evaluate('document.body.scrollWidth <= innerWidth')
    manager.set_viewport_size({'width':1100,'height':760})
    manager.once('dialog',lambda dialog:dialog.accept())
    row.get_by_role('button',name='Eliminar',exact=True).click()
    expect(manager.locator('#mappingRows')).not_to_contain_text('se ge si')
    assert list(path.parent.glob(path.name+'.bak-*'))
    widget.locator('#discretion').click()
    expect(manager.locator('#dictionaryDialog')).not_to_be_visible()
    expect(manager.locator('body')).to_have_class('private')
    widget.locator('#discretion').click()
    expect(manager.locator('body')).not_to_have_class('private')


def validate_review_browser(manager,widget,evidence):
    sid='b'*32
    manager.locator(f'[data-session-id="{sid}"]').click()
    manager.locator('[data-tab=suspects]').click()
    expect(manager.locator('#reviewState')).to_contain_text('Pulsa Buscar')
    assert not app.reviews.path(sid).exists()
    manager.locator('#scanWords').click()
    expect(manager.locator('#reviewState')).to_contain_text('3 pendientes',timeout=15000)
    row=lambda word:manager.locator('.review-card').filter(has=manager.get_by_text(word,exact=True))
    expect(row('xqzblorf')).to_contain_text('2 apariciones')
    row('xqzblorf').get_by_role('button',name='Ignorar en esta reunión',exact=True).click()
    expect(manager.locator('#reviewState')).to_contain_text('2 pendientes')
    manager.locator('#reviewStatus').select_option('reviewed')
    row('xqzblorf').get_by_role('button',name='Deshacer ignorar',exact=True).click()
    expect(manager.locator('#reviewState')).to_contain_text('3 pendientes')
    manager.locator('#reviewStatus').select_option('pending')
    row('xqzblorf').get_by_role('button',name='Ignorar en esta reunión',exact=True).click()
    expect(manager.locator('#reviewState')).to_contain_text('2 pendientes')
    row('zqword').get_by_role('button',name='Agregar al diccionario personal',exact=True).click()
    expect(manager.locator('#reviewState')).to_contain_text('1 pendientes')
    manager.locator('#reviewStatus').select_option('reviewed')
    row('zqword').get_by_role('button',name='Gestionar mis términos',exact=True).click()
    expect(manager.locator('#personalTermsRows')).to_contain_text('zqword')
    manager.locator('#personalTerm').fill('Qzmanual');manager.locator('#personalTerm').press('Enter')
    expect(manager.locator('#personalTermsRows')).to_contain_text('Qzmanual')
    manager.locator('#personalTerm').fill('QZMANUAL');manager.locator('#personalTerm').press('Enter')
    expect(manager.locator('#personalTermsError')).to_contain_text('ya está')
    expect(manager.locator('#personalTerm')).to_have_value('QZMANUAL')
    manager.locator('#personalTerm').fill('')
    manager.screenshot(path=str(evidence/'personal-terms.png'))
    manual=manager.locator('#personalTermsRows tr').filter(has=manager.get_by_text('Qzmanual',exact=True))
    manager.once('dialog',lambda d:d.accept());manual.get_by_role('button',name='Eliminar',exact=True).click()
    expect(manager.locator('#personalTermsRows')).not_to_contain_text('Qzmanual')
    manager.get_by_label('Cerrar diccionario').click()
    manager.locator('#reviewStatus').select_option('pending')
    row('qzthird').get_by_role('button',name='Crear mapping',exact=True).click()
    expect(manager.locator('#mappingSource')).to_have_value('qzthird')
    manager.locator('#mappingTarget').fill('Correcto');manager.locator('#saveMapping').click()
    expect(manager.locator('#mappingStatus')).to_contain_text('Término atendido',timeout=10000)
    manager.get_by_label('Cerrar diccionario').click()
    manager.locator('#scanWords').click()
    expect(manager.locator('#reviewState')).to_contain_text('0 pendientes',timeout=10000)
    manager.locator('#reviewStatus').select_option('reviewed')
    expect(manager.locator('.review-card')).to_have_count(3)
    expect(row('xqzblorf')).to_contain_text('Ignorada')
    expect(row('zqword')).to_contain_text('Agregada')
    expect(row('qzthird')).to_contain_text('Mapping creado')
    manager.screenshot(path=str(evidence/'review-words.png'))
    manager.set_viewport_size({'width':760,'height':500})
    manager.screenshot(path=str(evidence/'review-words-760.png'))
    assert manager.evaluate('document.body.scrollWidth <= innerWidth')
    manager.set_viewport_size({'width':1100,'height':760})
    row('xqzblorf').locator('.review-context').first.click()
    expect(manager.locator('#tab-transcript')).to_be_visible()
    expect(manager.locator('.review-focus')).to_be_visible()
    assert app.personal_terms.path().is_relative_to(app.settings.PATH.parent)


def validate_session_edits_browser(manager,widget,evidence):
    sid='b'*32
    manager.locator('[data-tab=transcript]').click()
    manager.locator('#renameSession').click()
    expect(manager.locator('#renameDialog')).to_be_visible()
    manager.locator('#savedTitle').fill('á'*121)
    expect(manager.locator('#saveRename')).to_be_disabled()
    expect(manager.locator('#savedTitle')).to_have_value('á'*121)
    manager.locator('#savedTitle').fill('Reunión revisada ágil')
    manager.locator('#savedTitle').press('Enter')
    expect(manager.locator('#renameDialog')).not_to_be_visible()
    expect(manager.locator('#sessionTitle')).to_have_text('Reunión revisada ágil')
    expect(manager.locator(f'[data-session-id="{sid}"]')).to_contain_text('Reunión revisada ágil')
    manager.locator('#reapplyMappings').click()
    expect(manager.locator('#textEditChanges')).to_contain_text('Correcto')
    manager.screenshot(path=str(evidence/'text-mapping-preview.png'))
    path=app.current_corrections().paths[0]
    with path.open('a',encoding='utf-8') as file:file.write('# Cambio externo tras vista previa\n')
    manager.locator('#confirmTextEdit').click()
    expect(manager.locator('#textEditError')).to_contain_text('cambiaron')
    expect(manager.locator('#textEditChanges')).to_contain_text('Correcto')
    manager.locator('#recalculateEdit').click()
    expect(manager.locator('#confirmTextEdit')).to_be_enabled()
    manager.locator('#confirmTextEdit').click()
    expect(manager.locator('#textEditDialog')).not_to_be_visible()
    expect(manager.locator('#transcript')).to_contain_text('Correcto')
    manager.locator('[data-tab=suspects]').click()
    expect(manager.locator('#reviewStale')).to_be_visible()
    manager.locator('[data-tab=transcript]').click()
    manager.locator('#restoreTranscript').click()
    expect(manager.locator('#textEditHeading')).to_have_text('Restaurar texto anterior')
    expect(manager.locator('#confirmTextEdit')).to_be_enabled()
    manager.locator('#confirmTextEdit').click()
    expect(manager.locator('#textEditDialog')).not_to_be_visible()
    expect(manager.locator('#transcript')).to_contain_text('qzthird')
    expect(manager.locator('#sessionTitle')).to_have_text('Reunión revisada ágil')
    manager.locator('#renameSession').click()
    expect(manager.locator('#renameDialog')).to_be_visible()
    widget.locator('#discretion').click()
    expect(manager.locator('#renameDialog')).not_to_be_visible()
    expect(widget.locator('body')).to_have_class('discreet')
    assert widget.evaluate('Math.abs(document.body.getBoundingClientRect().height-64)<1')
    assert widget.evaluate('document.body.getBoundingClientRect().width<=320')
    widget.screenshot(path=str(evidence/'widget-compact-ready.png'))
    widget.locator('#discretion').click()
    expect(widget.locator('body')).not_to_have_class('discreet')


def validate_browser(url, evidence, provider_url):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge',headless=True)
        context = browser.new_context()
        widget = context.new_page(); widget.set_viewport_size({'width':360,'height':540})
        errors=[]
        context.on('page',lambda page:page.on('pageerror',lambda e:errors.append(str(e))))
        widget.on('pageerror',lambda e:errors.append(str(e)))
        widget.goto(url)
        expect(widget.locator('#model')).to_have_value('fake')
        assert widget.locator('#playback, #preparation, #loadSession').count() == 0
        with context.expect_page() as opened: widget.locator('#sessions').click()
        manager=opened.value; manager.set_viewport_size({'width':1100,'height':760})
        expect(manager.locator('.session-row').first).to_be_visible()
        # The named target must reuse the existing manager tab.
        count=len(context.pages); widget.locator('#sessions').click(); manager.wait_for_timeout(150); assert len(context.pages)==count
        validate_dictionary_browser(manager,widget,evidence)
        validate_review_browser(manager,widget,evidence)
        validate_session_edits_browser(manager,widget,evidence)
        manager.locator('#configure').click()
        manager.locator('#baseUrl').fill(provider_url)
        manager.locator('#apiModel').fill('fake')
        manager.locator('#apiKey').fill('fake-ui-key')
        manager.locator('#automatic').check()
        manager.locator('#settingsForm button[type=submit]').click()
        expect(manager.locator('#settingsState')).to_have_text('Configuración guardada')
        expect(manager.locator('#apiKey')).to_have_value('')
        manager.locator('#testApi').click()
        expect(manager.locator('#settingsState')).to_have_text('Conexión y respuesta compatibles')
        manager.get_by_label('Cerrar configuración').click()
        manager.locator('#prepare').click()
        manager.locator('#draftContext').fill('á'*2001)
        expect(manager.locator('#savePreparation')).to_be_disabled()
        expect(manager.locator('#draftContext')).to_have_value('á'*2001)
        manager.locator('#draftTitle').fill('Reunión sintética nueva')
        manager.locator('#draftContext').fill('SGSI · José · contexto privado')
        manager.locator('#draftTemplate').select_option('interview')
        manager.locator('#draftBackup').check()
        manager.locator('#useMic').check()
        manager.locator('#probeStart').click()
        expect(manager.locator('#probeResults')).to_contain_text('Señal detectada')
        manager.screenshot(path=str(evidence/'manager-preparation.png'))
        manager.locator('#savePreparation').click()
        expect(manager.locator('#preparationDialog')).not_to_be_visible()
        expect(widget.locator('#title')).to_have_text('Reunión sintética nueva')
        widget.locator('#toggle').click()
        expect(widget.locator('#tlabel')).to_have_text('Pausar')
        sid=app.session.session_id
        expect(widget.locator('#log .line').first).to_be_visible(timeout=10000)
        widget.locator('#mark').focus(); widget.keyboard.press('Enter')
        # Consult an older session while the widget keeps recording.
        old='b'*32
        manager.locator(f'[data-session-id="{old}"]').click()
        expect(manager.locator('#audioStatus')).to_contain_text('bloqueada')
        assert app.session.session_id==sid and app.session.state=='recording'
        manager.locator('[data-tab=suspects]').click();manager.locator('#scanWords').click()
        expect(manager.locator('#reviewState')).to_contain_text('0 pendientes',timeout=10000)
        assert app.session.session_id==sid and app.session.state=='recording'
        manager.locator('[data-tab=transcript]').click()

        manager.locator('#privacy').click()
        expect(widget.locator('body')).to_have_class('discreet')
        expect(manager.locator('body')).to_have_class('private')
        expect(manager.locator('#sessionTitle')).to_be_hidden()
        widget.screenshot(path=str(evidence/'widget-private.png'))
        widget.reload(); expect(widget.locator('body')).to_have_class('discreet')
        manager.locator('#privacy').click()
        widget.locator('#toggle').click(); expect(widget.locator('#tlabel')).to_have_text('Reanudar')
        widget.locator('#toggle').click(); expect(widget.locator('#tlabel')).to_have_text('Pausar')
        widget.locator('#finish').click(); expect(widget.locator('#tlabel')).to_have_text('Nueva sesión',timeout=10000)
        manager.locator(f'[data-session-id="{sid}"]').click()
        expect(manager.locator('#playback')).to_be_visible()
        manager.locator('[data-tab=analysis]').click()
        expect(manager.locator('#analysisState')).to_contain_text('Completado',timeout=15000)
        expect(manager.locator('#analysisResult')).to_contain_text('Resultado sintético')
        expect(manager.locator('#analysisTemplate')).to_have_value('interview')
        manager.screenshot(path=str(evidence/'manager-analysis.png'))
        manager.locator('#showAnalysisDetails').click()
        expect(manager.locator('#analysisDetails .analysis-section').first).to_be_visible()
        manager.locator('#analysisDetails summary').first.focus();manager.keyboard.press('Enter')
        expect(manager.locator('#analysisDetails .analysis-section').first).to_have_attribute('open','')
        manager.screenshot(path=str(evidence/'manager-analysis-sections.png'))
        manager.locator('#analysisDetails .reference').first.click()
        expect(manager.locator('#tab-transcript')).to_be_visible()
        manager.locator('[data-tab=analysis]').click()
        from test_analysis import Provider
        Provider.status=429
        manager.locator('#analyze').click()
        expect(manager.locator('#analysisState')).to_contain_text('HTTP 429',timeout=15000)
        expect(manager.locator('#analysisResult')).to_contain_text('Resultado sintético')
        expect(manager.locator('#continueAnalysis')).to_be_visible()
        Provider.status=200
        config=app.analysis_config();config['requests_per_minute']=2
        app.settings.save({'analysis':config})
        with app.analysis_queue.condition:
            for job in app.analysis_queue.jobs:
                for request in job.get('checkpoint',{}).get('requests',[]):request['started_at']=time.time()-57
            app.analysis_queue._persist()
        manager.locator('#continueAnalysis').click()
        expect(manager.locator('#analysisState')).to_contain_text('Esperando cuota',timeout=5000)
        manager.screenshot(path=str(evidence/'manager-analysis-quota.png'))
        expect(manager.locator('#analysisState')).to_contain_text('Completado',timeout=15000)
        config['requests_per_minute']=None;app.settings.save({'analysis':config})
        manager.locator('#showSynthesis').click()
        manager.locator('[data-tab=transcript]').click()
        manager.evaluate("document.getElementById('player').play()")
        manager.wait_for_function("document.getElementById('player').currentTime > 0")
        manager.screenshot(path=str(evidence/'manager-transcript.png'))
        manager.locator('[data-tab=markers]').click()
        expect(manager.locator('#markers')).to_contain_text('Marcador 1')
        manager.once('dialog',lambda dialog:dialog.accept('Acuerdo revisado'))
        manager.locator('#markers').get_by_role('button',name='Renombrar',exact=True).click()
        expect(manager.locator('#markers')).to_contain_text('Acuerdo revisado')
        # A new capture must remain independent of manual analysis and selected sessions.
        widget.locator('#toggle').click(); expect(widget.locator('#tlabel')).to_have_text('Iniciar')
        widget.locator('#toggle').click(); expect(widget.locator('#tlabel')).to_have_text('Pausar')
        new_sid=app.session.session_id
        manager.locator(f'[data-session-id="{old}"]').click()
        manager.locator('[data-tab=analysis]').click()
        manager.locator('#analysisTemplate').select_option('class'); manager.locator('#analyze').click()
        expect(manager.locator('#analysisState')).to_contain_text('Completado',timeout=15000)
        assert app.session.session_id==new_sid and app.session.state=='recording'
        widget.locator('#finish').click(); expect(widget.locator('#tlabel')).to_have_text('Nueva sesión',timeout=10000)
        for width,height in [(300,360),(360,540)]:
            widget.set_viewport_size({'width':width,'height':height})
            widget.screenshot(path=str(evidence/f'widget-{width}.png'))
            assert widget.evaluate('document.body.scrollWidth <= innerWidth')
            box=widget.locator('#toggle').bounding_box(); assert box['y']+box['height']<=height
        manager.set_viewport_size({'width':760,'height':500}); manager.screenshot(path=str(evidence/'manager-760.png'))
        assert manager.evaluate('document.body.scrollWidth <= innerWidth')
        assert not errors,errors
        assert 'fake-ui-key' not in app.settings.PATH.read_text(encoding='utf-8')
        browser.close()
    print(json.dumps({'result':'OK','surface':'Windows Edge, widget + manager','screenshots':str(evidence)},ensure_ascii=False))


def main():
    from http.server import ThreadingHTTPServer
    from test_analysis import Credentials,Provider,make_session
    import analysis
    import recording
    with tempfile.TemporaryDirectory(prefix='transcriptor-ui-data-') as temporary:
        root=Path(temporary); evidence=Path(tempfile.mkdtemp(prefix='transcriptor-ui-evidence-'))
        output_folder=root/'sessions'; output_folder.mkdir()
        prefs=patch('app.settings.PATH',root/'settings.json'); prefs.start()
        output=patch('app.OUT_DIR',output_folder); output.start()
        mapping_file=root/'mappings.tsv'
        mapping_file.write_text('# Archivo sintético de validación\nsi go\tSiigo\tword\tnocase\n',encoding='utf-8')
        app.settings.save({'dict_paths':str(mapping_file)})
        app.fixes=corrections.Corrections(str(mapping_file))
        app.session=app.Session(); device=Device()
        app.session.get_model=lambda _:SimpleNamespace(chunk=(.5,1),name='whisper',device='cpu',live_ok=False,transcribe=lambda *a,**kw:'Prueba sintética de sincronización.')
        app.repository=app.SessionRepository(app.output_dir,app.live_session,root/'index.json')
        historical=make_session(output_folder,'b'*32,text='hablábamos reuniones computers creating xqzblorf zqword qzthird xqzblorf')
        writer=recording.AudioBackup(historical.parent,lambda _:None)
        writer.submit('Otros',0,np.zeros(32000,dtype=np.float32));writer.close(2)
        data=json.loads(historical.read_text(encoding='utf-8'));data.update(parts=writer.manifest(),backup_enabled=True)
        recording.atomic_json(historical,data)
        (output_folder/'Antigua.txt').write_text('# Sesión antigua\n[00:00:01] [Yo] Ejemplo anterior.',encoding='utf-8')
        provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        provider_thread=threading.Thread(target=provider.serve_forever,daemon=True);provider_thread.start()
        provider_url=f'http://127.0.0.1:{provider.server_port}/v1'
        app.credentials=Credentials();app.credentials.set(provider_url,'fake-ui-key')
        if '--native' in sys.argv: app.settings.save({'analysis':{**analysis.DEFAULTS,'base_url':provider_url,'model':'fake','automatic':True}})
        app.analysis_queue=analysis.AnalysisQueue(app.repository,root/'queue.json',app.analysis_config,app.credentials,app.broadcast,lambda state:app.desktop.notify(state) if app.desktop else None)
        patches=[patch.dict(os.environ,{'LOCALAPPDATA':str(root/'profile')}),patch('app.audio.system_loopback',return_value=(device,False)),patch('app.audio.mic',return_value=(device,False)),
                 patch('app.audio.list_devices',return_value={'mics':[{'id':'mic','name':device.name}],'speakers':[{'id':'speaker','name':device.name}],'default_mic':'mic','default_speaker':'speaker'}),
                 patch('app.models.discover',return_value=[{'id':'fake','engine':'whisper','label':'Motor sintético','path':'fake'}])]
        for item in patches:item.start()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app.app,host='127.0.0.1',port=port,log_level='error'))
        worker=threading.Thread(target=server.run,daemon=True);worker.start()
        try:
            deadline=time.monotonic()+10
            while not server.started and time.monotonic()<deadline:time.sleep(.05)
            url=f'http://127.0.0.1:{port}'
            if '--native' in sys.argv:validate_native(url,evidence)
            else:validate_browser(url,evidence,provider_url)
        finally:
            app.desktop=None;app.window=None
            app.probe.stop();app.session.stop()
            if app.session.worker:app.session.worker.join(5)
            for review_thread in app.reviews.threads.values():review_thread.join(5)
            app.analysis_queue.stop()
            if app.analysis_queue.thread:app.analysis_queue.thread.join(5)
            server.should_exit=True;worker.join(5)
            provider.shutdown();provider.server_close();provider_thread.join(2)
            for item in reversed(patches):item.stop()
            output.stop();prefs.stop()


if __name__=='__main__':main()
