"""Documented visual tokens and calculated sRGB contrast ratios."""
PALETTE = [
    ('Fond', '--bg', '#080c1c'),
    ('Panneaux', '--surface', '#11172b'),
    ('Texte principal', '--ink', '#e8edff'),
    ('Texte secondaire', '--muted', '#a0aec9'),
    ('Cyan signal', '--cyan', '#67ddff'),
    ('Violet accent', '--purple', '#b6a5ff'),
    ('Disponible', '--green', '#79e7bd'),
    ('Incident', '--red', '#ff9bb0'),
    ('Bordure de contrôle', '--control-border', '#586784'),
]


def luminance(color):
    channels = [int(color[index:index+2], 16) / 255 for index in (1, 3, 5)]
    channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(foreground, background):
    values = sorted([luminance(foreground), luminance(background)])
    return (values[1] + .05) / (values[0] + .05)


def styleguide_context():
    pairs = [
        ('Texte principal', '#e8edff', '#11172b', 4.5),
        ('Texte secondaire', '#a0aec9', '#11172b', 4.5),
        ('Lien violet', '#b6a5ff', '#11172b', 4.5),
        ('Donnée cyan', '#67ddff', '#11172b', 4.5),
        ('Bouton principal', '#ffffff', '#6748d6', 4.5),
        ('Bouton principal au survol', '#ffffff', '#7657e6', 4.5),
        ('État disponible', '#79e7bd', '#12362e', 4.5),
        ('État incident', '#ff9bb0', '#3b1e2b', 4.5),
        ('État suspendu', '#f1d48a', '#352d1b', 4.5),
        ('Bordure sur panneau', '#586784', '#11172b', 3.0),
    ]
    return {
        'palette': [{'label': label, 'token': token, 'hex': value} for label, token, value in PALETTE],
        'contrasts': [{'label': label, 'fg': fg, 'bg': bg, 'ratio': f'{contrast(fg, bg):.2f}',
                       'minimum': minimum, 'passes': contrast(fg, bg) >= minimum} for label, fg, bg, minimum in pairs],
    }
