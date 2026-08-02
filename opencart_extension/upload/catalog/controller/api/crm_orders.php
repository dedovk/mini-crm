<?php
class ControllerApiCrmOrders extends Controller {
    public function index() {
        $json = array('success' => false, 'orders' => array());

        $provided_key = '';
        if (isset($this->request->server['HTTP_X_CRM_API_KEY'])) {
            $provided_key = trim($this->request->server['HTTP_X_CRM_API_KEY']);
        }

        if (!$provided_key) {
            $this->response->addHeader('HTTP/1.1 401 Unauthorized');
            $json['error'] = 'missing_api_key';
            return $this->sendJson($json);
        }

        $api_query = $this->db->query(
            "SELECT api_id FROM `" . DB_PREFIX . "api` " .
            "WHERE `key` = '" . $this->db->escape($provided_key) . "' AND status = '1' LIMIT 1"
        );
        if (!$api_query->num_rows) {
            $this->response->addHeader('HTTP/1.1 403 Forbidden');
            $json['error'] = 'invalid_api_key';
            return $this->sendJson($json);
        }

        $changed_from = isset($this->request->get['changed_from'])
            ? $this->request->get['changed_from']
            : date('Y-m-d H:i:s', strtotime('-30 days'));
        $timestamp = strtotime($changed_from);
        if ($timestamp === false) {
            $this->response->addHeader('HTTP/1.1 400 Bad Request');
            $json['error'] = 'invalid_changed_from';
            return $this->sendJson($json);
        }
        $changed_from = date('Y-m-d H:i:s', $timestamp);
        $limit = isset($this->request->get['limit']) ? (int)$this->request->get['limit'] : 500;
        $limit = max(1, min($limit, 500));
        $offset = isset($this->request->get['offset']) ? (int)$this->request->get['offset'] : 0;
        $offset = max(0, $offset);

        $orders_query = $this->db->query(
            "SELECT o.*, os.name AS order_status " .
            "FROM `" . DB_PREFIX . "order` o " .
            "LEFT JOIN `" . DB_PREFIX . "order_status` os " .
            "ON (o.order_status_id = os.order_status_id AND os.language_id = '" . (int)$this->config->get('config_language_id') . "') " .
            "WHERE o.order_status_id > 0 " .
            "AND o.date_modified >= '" . $this->db->escape($changed_from) . "' " .
            "ORDER BY o.date_modified ASC LIMIT " . $offset . "," . $limit
        );

        foreach ($orders_query->rows as $order) {
            $products_query = $this->db->query(
                "SELECT order_product_id, product_id, name, model, quantity, price, total " .
                "FROM `" . DB_PREFIX . "order_product` WHERE order_id = '" . (int)$order['order_id'] . "'"
            );
            $history_query = $this->db->query(
                "SELECT comment FROM `" . DB_PREFIX . "order_history` " .
                "WHERE order_id = '" . (int)$order['order_id'] . "' AND comment <> '' ORDER BY date_added ASC"
            );
            $history_comments = array();
            foreach ($history_query->rows as $history) {
                $history_comments[] = $history['comment'];
            }
            $order['products'] = $products_query->rows;
            $order['history_comments'] = $history_comments;
            $json['orders'][] = $order;
        }

        $json['success'] = true;
        $json['count'] = count($json['orders']);
        return $this->sendJson($json);
    }

    private function sendJson($json) {
        $this->response->addHeader('Content-Type: application/json; charset=utf-8');
        $this->response->setOutput(json_encode($json));
    }
}
