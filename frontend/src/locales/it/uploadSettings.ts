export const uploadSettings = {
  'uploadSettings.screenshotsTitle': 'Screenshot',
  'uploadSettings.screenshotsDescription': 'Vale per il prossimo upload preparato, non in modo retroattivo su quelli già pronti.',
  'uploadSettings.screenshotCountLabel': 'Numero di screenshot',
  'uploadSettings.screenshotCountDescription': 'Fotogrammi equidistanti, esclude il primo/ultimo 5% della durata.',
  'uploadSettings.tonemapLabel': 'Tonemap HDR negli screenshot',
  'uploadSettings.tonemapDescription':
    'Converte HDR→SDR (algoritmo mobius) prima di catturare gli screenshot — senza, una sorgente HDR sembra slavata/scura una volta interpretata come SDR.',
  'uploadSettings.descriptionTitle': 'Descrizione',
  'uploadSettings.descriptionHeaderLabel': 'Intestazione della descrizione',
  'uploadSettings.descriptionHeaderHelp':
    'Testo (es. BBCode) messo prima della descrizione generata dal template del tracker — lascia vuoto per non aggiungere niente.',
  'uploadSettings.descriptionHeaderSaved': 'Intestazione salvata.',
  'uploadSettings.saveHeaderButton': 'Salva intestazione',
  'uploadSettings.signatureLabel': 'Firma',
  'uploadSettings.signatureHelp':
    'Testo (es. BBCode) aggiunto alla fine di ogni descrizione — lascia vuoto per non aggiungere niente. Dopo segue sempre una breve riga "Uploaded with Nazgarr" con la versione.',
  'uploadSettings.signatureSaved': 'Firma salvata.',
  'uploadSettings.saveSignatureButton': 'Salva firma',
  'uploadSettings.imageHostsTitle': 'Host di immagini',
  'uploadSettings.hostPlugin':
    'plugin',
  'uploadSettings.hostStatus.ready':
    'pronto',
  'uploadSettings.hostStatus.missing':
    'manca la API key',
  'uploadSettings.hostsRemoved':
    'Con l’aggiornamento questi host non ci sono più: {hosts}. Inserisci la API key di almeno uno degli host qui sotto, o installa un plugin per quello che usavi.',
  'uploadSettings.morePlugins':
    'Ogni host è un plugin: si spegne, o se ne aggiungono altri, in Impostazioni › Plugin. Un host Chevereto è poche righe.',
  'uploadSettings.imageHostsDescription':
    'Si prova il primo host acceso e con la API key, se fallisce il successivo. Trascina per cambiare l’ordine, tocca un host per la sua chiave.',
  'uploadSettings.priorityOrderSaved': 'Ordine di priorità salvato.',
  'uploadSettings.dragToReorder': 'Trascina per riordinare {label}',
  'uploadSettings.fileNamesTitle': 'Nomi dei file nel torrent',
  'uploadSettings.fileNamesDescription':
    'Quando su un client non c’è un torrent con gli stessi file (hardlink), i file di un nuovo upload prendono un nome costruito da questo pattern, separato da punti come una release: niente ":" né accenti. Il titolo è quello che usano i tracker dell’upload, se sono d’accordo (per ITT quello in italiano); se no l’originale. Ogni upload può comunque tenere i nomi originali.',
  'uploadSettings.fileNamesReset': 'Torna al predefinito',
  'uploadSettings.releasesTitle': 'Le tue release',
  'uploadSettings.releasesDescription':
    'Per quello che rilasci tu: mettilo nella cartella osservata di un disco (Impostazioni › Archiviazione) e l’upload parte da solo, fino alla decisione. Quando è in seed su ogni tracker, viene spostato nella cartella delle release: la cartella osservata è solo un ingresso. Finché un tracker è fallito resta lì, così "Riprova" ha ancora i suoi file.',
  'uploadSettings.releaserLabel': 'Nome del releaser',
  'uploadSettings.releaserDescription': 'Il gruppo in fondo ai nomi degli upload dalla cartella osservata, e di ogni upload il cui nome non ha un gruppo (es. -NZG). Modificabile su ogni upload.',
  'uploadSettings.autoMatchLabel': 'Match TMDB automatico',
  'uploadSettings.splitIncompleteLabel': 'Stagioni incomplete in episodi',
  'uploadSettings.splitIncompleteDescription':
    'Una stagione che dalla cartella osservata arriva senza tutti gli episodi (es. i primi 2 di 8) diventa un upload per episodio invece di un season pack incompleto. Quando l’ultimo è in seed, la sua cartella si toglie con quello che resta (nfo, sample).',
  'uploadSettings.splitSeasonsLabel': 'Più stagioni in season pack',
  'uploadSettings.splitSeasonsDescription':
    'Una cartella con più stagioni che arriva nella cartella osservata (una serie intera) diventa un upload per stagione, ognuno con i soli suoi episodi, invece di un unico complete pack. Una stagione incompleta segue poi l’opzione qui sopra.',
  'uploadSettings.autoMatchDescription':
    'Per ogni upload, a mano o dalla cartella osservata: un match almeno così sicuro (0-1) viene confermato da solo; sotto, o quando due titoli si somigliano, aspetta te. Cambia match lo riporta indietro. 0 lo disattiva. Predefinito: 0.9.',
  'uploadSettings.identical.title': 'Torrent identico sul tracker',
  'uploadSettings.identical.description':
    'Quando il dupe check trova la stessa release già su un tracker, meglio un reseed di un nuovo upload. Un reseed arriva sempre e solo dopo un full hash check con ogni piece uguale.',
  'uploadSettings.identical.offTitle': 'Decidi tu',
  'uploadSettings.identical.offHelp': 'Il full hash check lo avvii tu e il reseed lo scegli alla decisione.',
  'uploadSettings.identical.verifyTitle': 'Hash check da solo',
  'uploadSettings.identical.verifyHelp':
    'Il full hash check parte appena l’analisi lo trova; la decisione resta tua, con il reseed già proposto.',
  'uploadSettings.identical.reseedTitle': 'Hash check e reseed da soli',
  'uploadSettings.identical.reseedHelp':
    'Al 100% il reseed si approva e va in coda da solo, se ogni tracker dell’upload ha una risposta certa (un reseed verificato, o i file già in seed lì). Un tracker con la sessione web fa prima il claim della resurrection di un torrent morto.',
  'uploadSettings.dupeThresholdLabel': 'Soglia del dupe check',
  'uploadSettings.dupeThresholdDescription':
    'Quanto di una release sul tracker deve essere noto e uguale alla tua (tipo, risoluzione, codec, HDR, lingue audio, stagione) per suggerire di non caricare (0-100). Sotto, è un possibile dupe e l’upload resta suggerito, con un avviso. Predefinito: 70.',
  'uploadSettings.autoRenameLabel': 'Rinomina i file in automatico',
  'uploadSettings.autoRenameHelp':
    'Acceso: i nomi del torrent in hardlink se c’è, altrimenti questo pattern per i file della libreria e le release della cartella osservata. Spento: ogni upload parte con i nomi originali; su ogni upload puoi comunque scegliere un’altra opzione.',
  'uploadSettings.singleFileLabel': 'File singolo al posto di una cartella con un file',
  'uploadSettings.singleFileHelp':
    "Quando l'unico file che entra nel torrent è dentro una cartella, il torrent è solo quel file, senza cartella. Sample e file di sistema non entrano mai nel torrent e non contano; un nfo sì, e il torrent resta una cartella.",
  'uploadSettings.singleFileFolderLabel': 'Cartella del file singolo',
  'uploadSettings.singleFileFolder.keep': 'Tieni la cartella',
  'uploadSettings.singleFileFolder.remove': 'Togli la cartella',
  'uploadSettings.singleFileFolderHelp.keep':
    'Il file seeda dentro la sua cartella nella cartella delle release, e il client punta lì. Una sorgente già nella cartella di seeding seeda dove si trova.',
  'uploadSettings.singleFileFolderHelp.remove':
    'Il file seeda direttamente nella cartella delle release, senza la sua cartella. Una sorgente già nella cartella di seeding seeda dove si trova, dentro la sua cartella.',
} as const
