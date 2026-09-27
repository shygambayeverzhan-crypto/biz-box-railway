import 'dotenv/config';
import { Telegraf, Markup, session } from 'telegraf';
import pg from 'pg';

const { Pool } = pg;

const BOT_TOKEN = process.env.BOT_TOKEN;
const DATABASE_URL = process.env.DATABASE_URL;
const ADMIN_TELEGRAM_ID = process.env.ADMIN_TELEGRAM_ID;

if (!BOT_TOKEN) throw new Error('BOT_TOKEN is not configured');
if (!DATABASE_URL) throw new Error('DATABASE_URL is not configured');

const bot = new Telegraf(BOT_TOKEN);
const pool = new Pool({
  connectionString: DATABASE_URL,
  ssl: DATABASE_URL.includes('railway') ? { rejectUnauthorized: false } : undefined
});

type SessionData = {
  step?: 'service' | 'description' | 'phone';
  service?: string;
  description?: string;
};

async function db(sql: string, params: any[] = []) {
  return pool.query(sql, params);
}

async function initDb() {
  await db(`
    CREATE TABLE IF NOT EXISTS service_users (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT UNIQUE NOT NULL,
      username TEXT,
      first_name TEXT,
      last_name TEXT,
      phone TEXT,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_leads (
      id BIGSERIAL PRIMARY KEY,
      telegram_id BIGINT NOT NULL,
      username TEXT,
      first_name TEXT,
      phone TEXT,
      service TEXT NOT NULL,
      description TEXT DEFAULT '',
      status TEXT DEFAULT 'new',
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
  `);
}

async function ensureUser(ctx: any) {
  const u = ctx.from;
  await db(
    `INSERT INTO service_users (telegram_id, username, first_name, last_name)
     VALUES ($1,$2,$3,$4)
     ON CONFLICT (telegram_id) DO UPDATE SET
       username=EXCLUDED.username,
       first_name=EXCLUDED.first_name,
       last_name=EXCLUDED.last_name,
       updated_at=NOW()`,
    [u.id, u.username ?? null, u.first_name ?? '', u.last_name ?? null]
  );
}

const mainMenu = () => Markup.inlineKeyboard([
  [Markup.button.callback('🔥 Мои услуги', 'services')],
  [Markup.button.callback('💼 Заказать услугу', 'order')],
  [Markup.button.callback('👤 Обо мне', 'about')],
  [Markup.button.callback('💬 Связаться со мной', 'contact')]
]);

const servicesMenu = () => Markup.inlineKeyboard([
  [Markup.button.callback('🎬 Reels / Мобилография', 'svc_reels')],
  [Markup.button.callback('📱 SMM', 'svc_smm')],
  [Markup.button.callback('🤖 Telegram-боты', 'svc_bots')],
  [Markup.button.callback('🌐 Создание сайтов', 'svc_sites')],
  [Markup.button.callback('🎯 Таргет', 'svc_target')],
  [Markup.button.callback('✍️ Сценарии / Контент', 'svc_content')],
  [Markup.button.callback('🎥 Продакшн под ключ', 'svc_prod')],
  [Markup.button.callback('💼 Комплексное продвижение', 'svc_full')],
  [Markup.button.callback('⬅️ Назад', 'home')]
]);

const backMenu = () => Markup.inlineKeyboard([
  [Markup.button.callback('🔥 Все услуги', 'services')],
  [Markup.button.callback('💼 Заказать', 'order')],
  [Markup.button.callback('⬅️ Назад', 'home')]
]);

const serviceData: Record<string, { title: string; text: string }> = {
  reels: {
    title: '🎬 REELS / МОБИЛОГРАФИЯ',
    text: 'Съёмка и монтаж коротких вертикальных видео для Instagram, TikTok и Shorts.\\n\\n📦 Пакет: 12 Reels — 100 000 ₸\\n\\nВключает съёмку, монтаж, цвет, звук и адаптацию под соцсети.'
  },
  smm: {
    title: '📱 SMM',
    text: 'Контент и продвижение соцсетей: стратегия, контент-план, Reels, оформление и работа с аудиторией.\\n\\n💰 Консультация по продвижению личного бренда — 25 000 ₸.'
  },
  bots: {
    title: '🤖 TELEGRAM-БОТЫ',
    text: 'Создание Telegram-ботов для продаж, заявок, автоматизации и клиентского сервиса.\\n\\n💰 Стоимость — от 50 000 ₸, зависит от функционала.'
  },
  sites: {
    title: '🌐 СОЗДАНИЕ САЙТОВ',
    text: 'Лендинги, сайты-визитки и веб-приложения под бизнес или личный бренд.\\n\\n💰 Стоимость — от 70 000 ₸.'
  },
  target: {
    title: '🎯 ТАРГЕТ',
    text: 'Настройка и ведение рекламы в Meta: аудитория, креативы, запуск, аналитика и оптимизация.\\n\\n💰 Настройка — 80 000 ₸.'
  },
  content: {
    title: '✍️ СЦЕНАРИИ / КОНТЕНТ',
    text: 'Сценарии для Reels, TikTok, рекламных роликов, Threads и коротких сериалов.\\n\\n💰 Стоимость рассчитывается под задачу.'
  },
  prod: {
    title: '🎥 ПРОДАКШН ПОД КЛЮЧ',
    text: 'Полный цикл: идея → сценарий → съёмка → монтаж → звук → публикация.\\n\\nПодходит брендам, экспертам, заведениям и бизнесу.'
  },
  full: {
    title: '💼 КОМПЛЕКСНОЕ ПРОДВИЖЕНИЕ',
    text: 'Объединяем контент, SMM, рекламу и автоматизацию в одну систему продвижения.\\n\\nФормат и бюджет подбираются после короткого брифа.'
  }
};

function serviceKeyFromAction(action: string) {
  return action.replace('svc_', '');
}

bot.use(session());

bot.start(async ctx => {
  await ensureUser(ctx);
  const u = ctx.from;
  const username = u.username ? `@${u.username}` : 'username не указан';

  await ctx.reply(
    `👋 Привет, ${u.first_name}!

Я бот Ержана. Здесь можно быстро посмотреть мои услуги, выбрать нужную и оставить заявку.

👤 Твой Telegram: ${username}

Никаких форм и длинных анкет — всё прямо в чате.`,
    mainMenu()
  );
});

bot.command('menu', async ctx => {
  await ensureUser(ctx);
  await ctx.reply('🚀 Главное меню', mainMenu());
});

bot.command('id', async ctx => {
  await ctx.reply(`Твой Telegram ID: ${ctx.from.id}`);
});

bot.action('home', async ctx => {
  await ctx.answerCbQuery();
  await ctx.editMessageText('🚀 Главное меню', mainMenu());
});

bot.action('services', async ctx => {
  await ctx.answerCbQuery();
  await ctx.editMessageText('🔥 МОИ УСЛУГИ\\n\\nВыбери, что тебе нужно:', servicesMenu());
});

for (const action of Object.keys(serviceData)) {
  bot.action(`svc_${action}`, async ctx => {
    const sessionData = ctx.session as SessionData;
    const data = serviceData[action];

    if (sessionData.step === 'service') {
      sessionData.service = data.title;
      sessionData.step = 'description';
      await ctx.answerCbQuery();
      await ctx.reply(
        `🔥 Выбрано: ${data.title}

Теперь напиши, что именно тебе нужно.
Например: «Нужно 10 Reels для магазина одежды, хотим снять за 2 дня».`
      );
      return;
    }

    await ctx.answerCbQuery();
    await ctx.editMessageText(`${data.title}\\n\\n${data.text}`, backMenu());
  });
}

bot.action('about', async ctx => {
  await ctx.answerCbQuery();
  await ctx.editMessageText(
    '👤 ОБО МНЕ\\n\\nЕржан — мобильный видеограф, продюсер и digital-специалист из Астаны.\\n\\nСоздаю контент, сайты, Telegram-ботов и системы продвижения под конкретную задачу бизнеса.',
    backMenu()
  );
});

bot.action('contact', async ctx => {
  await ctx.answerCbQuery();
  await ctx.reply(
    '💬 Можешь написать мне напрямую или оставить заявку через бота.\\n\\nНажми «Заказать», если хочешь, чтобы я сам связался с тобой.',
    backMenu()
  );
});

bot.action('order', async ctx => {
  await ctx.answerCbQuery();
  const sessionData = ctx.session as SessionData;
  sessionData.step = 'service';
  sessionData.service = undefined;
  sessionData.description = undefined;

  await ctx.editMessageText(
    '💼 ЗАКАЗАТЬ УСЛУГУ\\n\\nВыбери, что тебе нужно:',
    servicesMenu()
  );
});

for (const action of Object.keys(serviceData)) {
  bot.action(`svc_${action}`, async ctx => {
    const sessionData = ctx.session as SessionData;
    if (sessionData.step !== 'service') return;

    sessionData.service = serviceData[action].title;
    sessionData.step = 'description';

    await ctx.answerCbQuery();
    await ctx.reply(
      `🔥 Выбрано: ${serviceData[action].title}

Теперь напиши, что именно тебе нужно.
Например: «Нужно 10 Reels для магазина одежды, хотим снять за 2 дня».`
    );
  });
}

bot.on('text', async ctx => {
  const sessionData = ctx.session as SessionData;

  if (ctx.message.text.startsWith('/')) return;

  if (sessionData.step === 'description') {
    sessionData.description = ctx.message.text.trim();
    sessionData.step = 'phone';

    await ctx.reply(
      '📱 Оставь номер телефона для связи или нажми кнопку «Пропустить».',
      Markup.keyboard([
        [Markup.button.contactRequest('📲 Отправить номер')],
        ['Пропустить']
      ]).resize().oneTime()
    );
    return;
  }

  if (sessionData.step === 'phone' && ctx.message.text === 'Пропустить') {
    await saveLead(ctx, null);
    return;
  }

  await ctx.reply('Выбери нужный раздел:', mainMenu());
});

bot.on('contact', async ctx => {
  const sessionData = ctx.session as SessionData;
  if (sessionData.step !== 'phone') return;

  await saveLead(ctx, ctx.message.contact.phone_number);
});

async function saveLead(ctx: any, phone: string | null) {
  const s = ctx.session as SessionData;
  const u = ctx.from;

  if (!s.service || !s.description) {
    s.step = undefined;
    await ctx.reply('Давай начнём заново — выбери услугу.', mainMenu());
    return;
  }

  await db(
    `INSERT INTO service_leads
      (telegram_id, username, first_name, phone, service, description)
     VALUES ($1,$2,$3,$4,$5,$6)`,
    [u.id, u.username ?? null, u.first_name ?? '', phone, s.service, s.description]
  );

  await db(
    'UPDATE service_users SET phone=COALESCE($2,phone), updated_at=NOW() WHERE telegram_id=$1',
    [u.id, phone]
  );

  if (ADMIN_TELEGRAM_ID) {
    const username = u.username ? `@${u.username}` : 'не указан';
    await bot.telegram.sendMessage(
      ADMIN_TELEGRAM_ID,
      `🔔 НОВАЯ ЗАЯВКА

👤 ${u.first_name} ${u.last_name ?? ''}
🔗 ${username}
🆔 ${u.id}
📱 ${phone ?? 'не указан'}

🔥 Услуга:
${s.service}

📝 Задача:
${s.description}`
    ).catch(err => console.error('ADMIN NOTIFY ERROR', err));
  }

  s.step = undefined;
  s.service = undefined;
  s.description = undefined;

  await ctx.reply(
    '✅ Заявка отправлена!

Спасибо. Я получил задачу и свяжусь с тобой для уточнения деталей.',
    Markup.removeKeyboard()
  );
  await ctx.reply('Если хочешь посмотреть другие услуги:', mainMenu());
}

bot.catch((err, ctx) => {
  console.error('BOT ERROR', err);
  ctx.reply('⚠️ Произошла ошибка. Попробуй ещё раз.').catch(() => {});
});

async function main() {
  await initDb();

  await bot.telegram.setMyCommands([
    { command: 'start', description: 'Запустить бота' },
    { command: 'menu', description: 'Услуги и меню' },
    { command: 'id', description: 'Показать мой Telegram ID' }
  ]);

  await bot.launch();
  console.log('YERZHAN SERVICES BOT started');
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});

process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
