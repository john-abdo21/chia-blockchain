const { i18n } = require('@lingui/core');
const {
  da,
  de,
  en,
  fi,
  fr,
  it,
  ja,
  pt,
  ro,
  ru,
  sk,
  sv,
  zh,
  es,
} = require('make-plural/plurals');
const catalogDa = require('../locales/da/messages');
const catalogDe = require('../locales/de/messages');
const catalogEn = require('../locales/en/messages');
const catalogEs = require('../locales/es/messages');
const catalogFi = require('../locales/fi/messages');
const catalogFr = require('../locales/fr/messages');
const catalogIt = require('../locales/it/messages');
const catalogJa = require('../locales/ja/messages');
const catalogPt = require('../locales/pt/messages');
const catalogRo = require('../locales/ro/messages');
const catalogRu = require('../locales/ru/messages');
const catalogSk = require('../locales/sk/messages');
const catalogSv = require('../locales/sv/messages');
const catalogZh = require('../locales/zh/messages');
const catalogZhCN = require('../locales/zh-CN/messages');

i18n.loadLocaleData('da', { plurals: da });
i18n.loadLocaleData('de', { plurals: de });
i18n.loadLocaleData('en', { plurals: en });
i18n.loadLocaleData('es', { plurals: es });
i18n.loadLocaleData('fi', { plurals: fi });
i18n.loadLocaleData('fr', { plurals: fr });
i18n.loadLocaleData('it', { plurals: it });
i18n.loadLocaleData('ja', { plurals: ja });
i18n.loadLocaleData('pt', { plurals: pt });
i18n.loadLocaleData('ro', { plurals: ro });
i18n.loadLocaleData('ru', { plurals: ru });
i18n.loadLocaleData('sk', { plurals: sk });
i18n.loadLocaleData('sv', { plurals: sv });
i18n.loadLocaleData('zh', { plurals: zh });
i18n.loadLocaleData('zh-CN', { plurals: zh });
i18n.load('da', catalogDa.messages);
i18n.load('de', catalogDe.messages);
i18n.load('en', catalogEn.messages);
i18n.load('es', catalogEs.messages);
i18n.load('fi', catalogFi.messages);
i18n.load('fr', catalogFr.messages);
i18n.load('it', catalogIt.messages);
i18n.load('ja', catalogJa.messages);
i18n.load('pt', catalogPt.messages);
i18n.load('ro', catalogRo.messages);
i18n.load('ru', catalogRu.messages);
i18n.load('sk', catalogSk.messages);
i18n.load('sv', catalogSv.messages);
i18n.load('zh', catalogZh.messages);
i18n.load('zh-CN', catalogZhCN.messages);

i18n.activate('en');

module.exports = i18n;
