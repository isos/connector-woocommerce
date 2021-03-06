# -*- coding: utf-8 -*-
#
#
#    Tech-Receptives Solutions Pvt. Ltd.
#    Copyright (C) 2009-TODAY Tech-Receptives(<http://www.techreceptives.com>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import logging
import xmlrpclib

from openerp import models, fields, api
from openerp.addons.connector.exception import IDMissingInBackend
from openerp.addons.connector.queue.job import job
from openerp.addons.connector.unit.mapper import (mapping,
                                                  ImportMapper
                                                  )
from openerp.addons.connector.unit.synchronizer import (Exporter)
from openerp.addons.connector.event import on_record_write
from ..backend import woo
from ..connector import get_environment
from ..unit.backend_adapter import (GenericAdapter)
from ..unit.import_synchronizer import (DelayedBatchImporter, WooImporter)
_logger = logging.getLogger(__name__)

ORDER_STATUS_MAPPING = {
    'draft': "Pending Payment",
    'sent': "",
    'manual': "Processing",
    'progress': "",
    'shipping_except': "",
    'invoice_except': "",
    'done': "Completed",
    'cancel': "Cancelled",
    'waiting_date': ""
}


class woo_sale_order_status(models.Model):
    _name = 'woo.sale.order.status'
    _description = 'WooCommerce Sale Order Status'

    name = fields.Char('Name')
    desc = fields.Text('Description')


class SaleOrder(models.Model):

    _inherit = 'sale.order'
    status_id = fields.Many2one('woo.sale.order.status',
                                'WooCommerce Order Status')


class WooSaleOrder(models.Model):
    _name = 'woo.sale.order'
    _inherit = 'woo.binding'
    _inherits = {'sale.order': 'openerp_id'}
    _description = 'Woo Sale Order'

    _rec_name = 'name'

    status_id = fields.Many2one('woo.sale.order.status',
                                'WooCommerce Order Status')

    openerp_id = fields.Many2one(comodel_name='sale.order',
                                 string='Sale Order',
                                 required=True,
                                 ondelete='cascade')
    woo_order_line_ids = fields.One2many(
        comodel_name='woo.sale.order.line',
        inverse_name='woo_order_id',
        string='Woo Order Lines'
    )
    backend_id = fields.Many2one(
        comodel_name='wc.backend',
        string='Woo Backend',
        store=True,
        readonly=False,
        required=True,
    )


class WooSaleOrderLine(models.Model):
    _name = 'woo.sale.order.line'
    _inherits = {'sale.order.line': 'openerp_id'}

    woo_order_id = fields.Many2one(comodel_name='woo.sale.order',
                                   string='Woo Sale Order',
                                   required=True,
                                   ondelete='cascade',
                                   select=True)

    openerp_id = fields.Many2one(comodel_name='sale.order.line',
                                 string='Sale Order Line',
                                 required=True,
                                 ondelete='cascade')

    backend_id = fields.Many2one(
        related='woo_order_id.backend_id',
        string='Woo Backend',
        readonly=True,
        store=True,
        required=False,
    )

    @api.model
    def create(self, vals):
        woo_order_id = vals['woo_order_id']
        binding = self.env['woo.sale.order'].browse(woo_order_id)
        vals['order_id'] = binding.openerp_id.id
        binding = super(WooSaleOrderLine, self).create(vals)
        return binding


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    woo_bind_ids = fields.One2many(
        comodel_name='woo.sale.order.line',
        inverse_name='openerp_id',
        string="WooCommerce Bindings",
    )


@woo
class SaleOrderLineImportMapper(ImportMapper):
    _model_name = 'woo.sale.order.line'

    direct = [('quantity', 'product_uom_qty'),
              ('quantity', 'product_uos_qty'),
              ('name', 'name'),
              ('price', 'price_unit')
              ]

    @mapping
    def product_id(self, record):
        if 'parent_id' in record and record['parent_id']:
            binder = self.binder_for('woo.product.combination')
            product_id = binder.to_openerp(record['product_id'], unwrap=True)

        else:
            binder = self.binder_for('woo.product.template')
            product_id = binder.to_openerp(record['product_id'], unwrap=True)
            product = self.env['product.product'].search(
                [('product_tmpl_id', '=', product_id)])
            product_id = product.id
        assert product_id is not None, (
            "product_id %s should have been imported in "
            "SaleOrderImporter._import_dependencies" % record['product_id'])
        return {'product_id': product_id}

    @mapping
    def qunatity(self, record):
        if record['quantity']:
            return {'product_uom_quantity': record['quantity']}


@woo
class SaleOrderAdapter(GenericAdapter):
    _model_name = 'woo.sale.order'
    _woo_model = 'orders/details'

    def _call(self, method, arguments):
        try:
            return super(SaleOrderAdapter, self)._call(method, arguments)
        except xmlrpclib.Fault as err:
            # this is the error in the Woo API
            # when the customer does not exist
            if err.faultCode == 102:
                raise IDMissingInBackend
            else:
                raise

    def search(self, filters=None, from_date=None, to_date=None):
        """ Search records according to some criteria and return a
        list of ids

        :rtype: list
        """
        if filters is None:
            filters = {}
        WOO_DATETIME_FORMAT = '%Y/%m/%d %H:%M:%S'
        dt_fmt = WOO_DATETIME_FORMAT
        if from_date is not None:
            # updated_at include the created records
            filters.setdefault('updated_at', {})
            filters['updated_at']['from'] = from_date.strftime(dt_fmt)
        if to_date is not None:
            filters.setdefault('updated_at', {})
            filters['updated_at']['to'] = to_date.strftime(dt_fmt)

        return self._call('orders/list?filter[limit]=9999',
                          [filters] if filters else [{}])

    def update_sale_state(self, id, data):
        # product_stock.update is too slow
        return self._call_order_status('order_status_update', [int(id), data])


@woo
class SaleOrderBatchImporter(DelayedBatchImporter):

    """ Import the WooCommerce Partners.

    For every partner in the list, a delayed job is created.
    """
    _model_name = ['woo.sale.order']

    def _import_record(self, woo_id, priority=None):
        """ Delay a job for the import """
        super(SaleOrderBatchImporter, self)._import_record(
            woo_id, priority=priority)

    def update_existing_order(self, woo_sale_order, record_id):
        """ Enter Your logic for Existing Sale Order """
        return True

    def run(self, filters=None):
        """ Run the synchronization """
#
        from_date = filters.pop('from_date', None)
        to_date = filters.pop('to_date', None)
        record_ids = self.backend_adapter.search(
            filters,
            from_date=from_date,
            to_date=to_date,
        )
        order_ids = []
        for record_id in record_ids:
            woo_sale_order = self.env['woo.sale.order'].search(
                [('woo_id', '=', record_id)])
            if woo_sale_order:
                self.update_existing_order(woo_sale_order[0], record_id)
            else:
                order_ids.append(record_id)
        _logger.info('search for woo partners %s returned %s',
                     filters, record_ids)
        for record_id in order_ids:
            self._import_record(record_id, 50)


SaleOrderBatchImporter = SaleOrderBatchImporter
#


@woo
class SaleOrderImporter(WooImporter):
    _model_name = ['woo.sale.order']

    def _import_addresses(self):
        record = self.woo_record
        record = record['order']
        self._import_dependency(record['customer_id'],
                                'woo.res.partner')

    def _import_dependencies(self):
        """ Import the dependencies for the record"""
        record = self.woo_record

        self._import_addresses()
        record = record['items']
        for line in record:
            _logger.debug('line: %s', line)
            if 'parent_id' in line and line['parent_id']:
                self._import_dependency(line['parent_id'],
                                        'woo.product.template')
            else:
                self._import_dependency(line['product_id'],
                                        'woo.product.template')

    def _clean_woo_items(self, resource):
        """
        Method that clean the sale order line given by WooCommerce before
        importing it

        This method has to stay here because it allow to customize the
        behavior of the sale order.

        """
        child_items = {}  # key is the parent item id
        top_items = []

        # Group the childs with their parent
        for item in resource['order']['line_items']:
            if item.get('parent_item_id'):
                child_items.setdefault(item['parent_item_id'], []).append(item)
            else:
                top_items.append(item)

        all_items = []
        for top_item in top_items:
            all_items.append(top_item)
        resource['items'] = all_items
        return resource

    def _create(self, data):
        openerp_binding = super(SaleOrderImporter, self)._create(data)
        return openerp_binding

    def _after_import(self, binding):
        """ Hook called at the end of the import """
        return

    def _get_woo_data(self):
        """ Return the raw WooCommerce data for ``self.woo_id`` """
        record = super(SaleOrderImporter, self)._get_woo_data()
        # sometimes we need to clean woo items (ex : configurable
        # product in a sale)
        record = self._clean_woo_items(record)
        return record
SaleOrderImport = SaleOrderImporter


@woo
class SaleOrderImportMapper(ImportMapper):
    _model_name = 'woo.sale.order'

    children = [('items', 'woo_order_line_ids', 'woo.sale.order.line'),
                ]

    @mapping
    def status(self, record):
        if record['order']:
            rec = record['order']
            if rec['status']:
                status_id = self.env['woo.sale.order.status'].search(
                    [('name', '=', rec['status'])])
                if status_id:
                    return {'status_id': status_id[0].id}
                else:
                    status_id = self.env['woo.sale.order.status'].create({
                        'name': rec['status']
                    })
                    return {'status_id': status_id.id}
            else:
                return {'status_id': False}

    @mapping
    def total(self, record):
        if record['order']:
            rec = record['order']
            if rec['total']:
                return {'amount_total': rec['total']}

    @mapping
    def sub_total(self, record):
        if record['order']:
            rec = record['order']
            if rec['subtotal']:
                return {'amount_untaxed': rec['subtotal']}

    @mapping
    def delivery(self, record):
        if record['order'] and record['order']['shipping_lines']:
            rec = record['order']['shipping_lines'][0]
            if rec['method_id']:
                delivery_id = self.env['woo.delivery.carrier'].search(
                    [('woo_id', '=', rec['method_id'])])
                if delivery_id:
                    return {'carrier_id': delivery_id.openerp_id.id}

    @mapping
    def customer_id(self, record):
        if record['order']:
            rec = record['order']
            binder = self.binder_for('woo.res.partner')
            if rec['customer_id']:
                partner_id = binder.to_openerp(rec['customer_id'],
                                               unwrap=True) or False
#               customer_id = str(rec['customer_id'])
                assert partner_id, ("Please Check Customer Role \
                                    in WooCommerce")
                result = {'partner_id': partner_id}
                onchange_val = self.env['sale.order'].onchange_partner_id(
                    partner_id)
                result.update(onchange_val['value'])
            else:
                customer = rec['customer']['billing_address']
                country_id = False
                state_id = False
                if customer['country']:
                    country_id = self.env['res.country'].search(
                        [('code', '=', customer['country'])])
                    if country_id:
                        country_id = country_id.id
                if customer['state']:
                    state_id = self.env['res.country.state'].search(
                        [('code', '=', customer['state'])])
                    if state_id:
                        state_id = state_id.id
                name = customer['first_name'] + ' ' + customer['last_name']
                partner_dict = {
                    'name': name,
                    'city': customer['city'],
                    'phone': customer['phone'],
                    'zip': customer['postcode'],
                    'state_id': state_id,
                    'country_id': country_id
                }
                partner_id = self.env['res.partner'].create(partner_dict)
                partner_dict.update({
                    'backend_id': self.backend_record.id,
                    'openerp_id': partner_id.id,
                })
#                 woo_partner_id = self.env['woo.res.partner'].create(
#                     partner_dict)
                result = {'partner_id': partner_id.id}
                onchange_val = self.env['sale.order'].onchange_partner_id(
                    partner_id.id)
                result.update(onchange_val['value'])
            return result

    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}


@job(default_channel='root.woo')
def sale_order_import_batch(session, model_name, backend_id, filters=None):
    """ Prepare the import of Sale Order modified on Woo """
    env = get_environment(session, model_name, backend_id)
    importer = env.get_connector_unit(SaleOrderBatchImporter)
    importer.run(filters=filters)


@woo
class SaleStateExport(Exporter):
    _model_name = ['woo.sale.order']

    def run(self, woo_id, state):
        datas = {
            'order_history': {
                'id_order': woo_id,
                'id_order_state': state,
            }
        }
        self.backend_adapter.update_sale_state(woo_id, datas)


@on_record_write(model_names='sale.order')
def woo_sale_state_modified(session, model_name, record_id,
                            fields=None):
    if 'state' in fields:
        sale = session.browse(model_name, record_id)
        # a quick test to see if it is worth trying to export sale state
        new_state = ORDER_STATUS_MAPPING[sale.state]
        states = session.search('sale.order.state', [('name', '=', new_state)])
        if states:
            export_sale_state.delay(session, record_id, priority=20)
    return True


@job
def export_sale_state(session, record_id):
    inherit_model = 'woo.sale.order'
    sale_ids = session.search(inherit_model, [('openerp_id', '=', record_id)])
    if not isinstance(sale_ids, list):
        sale_ids = [sale_ids]
    for sale in session.browse(inherit_model, sale_ids):
        backend_id = sale.backend_id.id
        new_state = ORDER_STATUS_MAPPING[sale.state]
        state_ids = session.search(
            'sale.order.state', [('name', '=', new_state)])
        woo_state_ids = session.search(
            'woo.sale.order.state', [('openerp_id', '=', state_ids[0])])
        woo_state = session.browse(
            'woo.sale.order.state', woo_state_ids[0])
        env = get_environment(session, inherit_model, backend_id)
        sale_exporter = env.get_connector_unit(SaleStateExport)
        if woo_state:
            sale_exporter.run(
                sale.woo_id, woo_state.woo_id)
